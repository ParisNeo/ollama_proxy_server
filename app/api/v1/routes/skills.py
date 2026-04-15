import json
import logging
import io
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, Request, HTTPException, Form, UploadFile, File
from app.crud import server_crud
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.database.models import User
from app.schema.settings import AppSettingsModel
from app.api.v1.dependencies import validate_csrf_token, get_settings
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.core.skills_manager import SkillsManager
from app.core import knowledge_importer as kit
import re

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/skills", response_class=HTMLResponse, name="admin_skills")
async def admin_skills_page(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    """Renders the HTML UI for the Skills Library."""
    from app.api.v1.dependencies import get_csrf_token
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/skills.html", context)

@router.get("/api/skills", name="api_get_skills")
async def api_get_skills(admin_user: User = Depends(require_admin_user)):
    """Returns a JSON list of all configured skills."""
    skills = SkillsManager.get_all_skills()
    return JSONResponse(skills)

@router.post("/api/skills", name="api_save_skill", dependencies=[Depends(validate_csrf_token)])
async def api_save_skill(
    request: Request,
    filename: str = Form(...),
    content: str = Form(...),
    admin_user: User = Depends(require_admin_user)
):
    """Saves or updates a skill markdown file."""
    try:
        saved_name = SkillsManager.save_skill(filename, content)
        return {"success": True, "filename": saved_name}
    except Exception as e:
        logger.error(f"Error saving skill: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.delete("/api/skills/{filename}", name="api_delete_skill")
async def api_delete_skill(filename: str, request: Request, admin_user: User = Depends(require_admin_user)):
    """Deletes a skill by filename."""
    # Custom simple CSRF verification for DELETE via fetch
    from app.api.v1.dependencies import get_csrf_token
    import secrets
    
    token = request.headers.get("X-CSRF-Token")
    stored = await get_csrf_token(request)
    if not token or not stored or not secrets.compare_digest(token, stored):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")

    if SkillsManager.delete_skill(filename):
        return {"success": True}
    return JSONResponse({"error": "File not found"}, status_code=404)

@router.get("/api/skills/{filename}/export", name="api_export_skill")
async def api_export_skill(filename: str, admin_user: User = Depends(require_admin_user)):
    """Exports a skill as a Claude-compliant .skill archive (zip)."""
    try:
        zip_bytes = SkillsManager.export_skill_zip(filename)
        headers = {
            'Content-Disposition': f'attachment; filename="{filename.replace(".md", "")}.skill"'
        }
        return Response(content=zip_bytes, media_type="application/zip", headers=headers)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Skill not found")
    except Exception as e:
        logger.error(f"Error exporting skill: {e}")
        raise HTTPException(status_code=500, detail=str(e))

from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
from app.api.v1.dependencies import get_settings
import secrets
import base64
import pipmaster as pm
from pydantic import BaseModel

class SearchRequest(BaseModel):
    query: str
    provider: str  # 'wikipedia', 'arxiv', 'google', 'youtube', 'github', 'stackoverflow'
    depth: int = 0
    full_content: bool = False

async def extract_content(files: List[UploadFile]) -> str:
    # Auto-install parsers if missing
    pm.ensure_packages(["pypdf", "python-docx", "pdf2image", "Pillow"], verbose=True)
    import pypdf, docx
    from pdf2image import convert_from_bytes
    
    extracted = []
    for file in files:
        data = await file.read()
        if file.content_type.startswith("image/"):
            b64 = base64.b64encode(data).decode('utf-8')
            extracted.append(f"IMAGE: {file.filename}\n[img:{b64}]")
        elif file.filename.endswith(".pdf"):
            reader = pypdf.PdfReader(io.BytesIO(data))
            text = "\n".join([p.extract_text() for p in reader.pages])
            extracted.append(f"PDF: {file.filename}\n{text}")
        elif file.filename.endswith(".docx"):
            doc = docx.Document(io.BytesIO(data))
            text = "\n".join([p.text for p in doc.paragraphs])
            extracted.append(f"DOCX: {file.filename}\n{text}")
    return "\n\n".join(extracted)

from fastapi import Form, File, UploadFile
from typing import List, Optional

from fastapi.exceptions import RequestValidationError

@router.post("/api/skills/build", name="api_build_skill")
async def api_build_skill(
    request: Request,
    prompt: str = Form(...),
    csrf_token: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_db),
    app_settings: AppSettingsModel = Depends(get_settings),
    admin_user: User = Depends(require_admin_user)
):
    """Orchestrates building a new skill using the 'build-a-skill' persona."""
    # Validate CSRF manually
    from app.api.v1.dependencies import get_csrf_token
    import secrets
    stored_token = await get_csrf_token(request)
    if not stored_token or not secrets.compare_digest(csrf_token, stored_token):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")
        
    target_agent = app_settings.admin_agent_name
    
    if not target_agent:
        return JSONResponse(
            {"error": "No Management Agent set in Settings. Please go to Settings > Hub Orchestration and select an Agent."}, 
            status_code=503
        )
        
    file_context = ""
    if files and any(f.filename for f in files):
        try:
            file_context = await extract_content([f for f in files if f.filename])
        except Exception as e:
            logger.error(f"Failed to extract file content: {e}")
            return JSONResponse({"error": f"Failed to read attached files: {e}"}, status_code=500)

    # Get the "Build a Skill" skill instructions
    build_skill = next((s for s in SkillsManager.get_all_skills() if s["name"] == "build-a-skill" or "build-a-skill" in s["filename"]), None)
    
    # GROUNDED PROMPT ENGINEERING:
    # 1. PERSONA SHIFT: Clinical generator role.
    # 2. SCHEMA ENFORCEMENT: Mandatory fields defined as a contract.
    # 3. OUTPUT ANCHORING: First character must be '-'.
    strict_instruction = (
        "You are a clinical automation component. You output raw file data. Zero conversation.\n\n"
        "MANDATORY YAML FRONTMATTER FIELDS:\n"
        "- name: (A unique kebab-case string, e.g., 'advanced-code-cleaner')\n"
        "- description: (Detailed explanation of triggers and logic)\n"
        "- author: (The creator name)\n"
        "- version: (e.g., '1.0.0')\n"
        "- category: (e.g., 'productivity', 'coding')\n\n"
        "TEMPLATE STRUCTURE:\n"
        "---\n"
        "name: [unique-id]\n"
        "description: [text]\n"
        "author: [text]\n"
        "version: 1.0.0\n"
        "category: [text]\n"
        "---\n"
        "# [Title]\n"
        "[Markdown Body content]\n\n"
        "STRICT RULES:\n"
        "1. Start exactly with '---\\n'.\n"
        "2. Do NOT use 'new-skill' as the name. Create a specific, unique name based on the task.\n"
        "3. No code fences (```) around the entire output.\n"
        "4. No chat preamble or postscript.\n\n"
        "BASE LOGIC TO INCORPORATE:\n"
        f"{build_skill['raw'] if build_skill else 'Generate a lollms skill.'}"
    )

    # REINFORCEMENT: Combine prompt with reference context
    user_request = f"TASK: Generate a unique lollms skill for: {prompt}"
    if file_context:
        user_request += f"\n\nSOURCE MATERIAL:\n{file_context}"
    
    # 4. END-OF-PROMPT REINFORCEMENT: Final instructions are prioritized by model attention.
    user_request += "\n\nCRITICAL: Return the raw .md content ONLY. Start with '---' and end with the last character of the content. Zero conversational text allowed."
    
    messages = [
        {"role": "system", "content": strict_instruction},
        {"role": "user", "content": user_request}
    ]
    
    from app.core.events import event_manager, ProxyEvent
    build_id = f"sys_build_{secrets.token_hex(4)}"

    # PHASE 1: Initialization
    event_manager.emit(ProxyEvent("received", build_id, "Skill Builder", "Local", admin_user.username, error_message="Initializing Skill Architect..."))
    
    file_context = ""
    if files and any(f.filename for f in files):
        event_manager.emit(ProxyEvent("active", build_id, "Skill Builder", "Local", admin_user.username, error_message=f"Extracting context from {len(files)} files..."))
        try:
            file_context = await kit.extract_local_file_content([f for f in files if f.filename])
        except Exception as e:
            event_manager.emit(ProxyEvent("error", build_id, "Skill Builder", "Local", admin_user.username, error_message=f"File error: {str(e)}"))
            return JSONResponse({"error": f"Failed to read files: {e}"}, status_code=500)

    # PHASE 2: AI Generation
    event_manager.emit(ProxyEvent("active", build_id, "Skill Builder", target_agent, admin_user.username, error_message=f"Prompting Management Agent ({target_agent})..."))
    
    # (Existing prompt logic here...)
    build_skill = next((s for s in SkillsManager.get_all_skills() if s["name"] == "build-a-skill" or "build-a-skill" in s["filename"]), None)
    
    strict_instruction = (
        "You are a clinical automation component. You output raw file data. Zero conversation.\n\n"
        "MANDATORY YAML FRONTMATTER FIELDS:\n"
        "- name: (A unique kebab-case string, e.g., 'advanced-code-cleaner')\n"
        "- description: (Detailed explanation of triggers and logic)\n"
        "- author: (The creator name)\n"
        "- version: (e.g., '1.0.0')\n"
        "- category: (e.g., 'productivity', 'coding')\n\n"
        "TEMPLATE STRUCTURE:\n"
        "---\n"
        "name: [unique-id]\n"
        "description: [text]\n"
        "author: [text]\n"
        "version: 1.0.0\n"
        "category: [text]\n"
        "---\n"
        "# [Title]\n"
        "[Markdown Body content]\n\n"
        "STRICT RULES:\n"
        "1. Start exactly with '---\\n'.\n"
        "2. Do NOT use 'new-skill' as the name. Create a specific, unique name based on the task.\n"
        "3. No code fences (```) around the entire output.\n"
        "4. No chat preamble or postscript.\n\n"
        "BASE LOGIC TO INCORPORATE:\n"
        f"{build_skill['raw'] if build_skill else 'Generate a lollms skill.'}"
    )

    user_request = f"TASK: Generate a unique lollms skill for: {prompt}"
    if file_context:
        user_request += f"\n\nSOURCE MATERIAL:\n{file_context}"
    user_request += "\n\nCRITICAL: Return raw .md content ONLY. Start with '---'."

    messages = [
        {"role": "system", "content": strict_instruction},
        {"role": "user", "content": user_request}
    ]
    
    try:
        # --- PHASE 1: GENERATE YAML METADATA ---
        event_manager.emit(ProxyEvent("active", build_id, "Skill Builder", target_agent, admin_user.username, error_message="Step 1/2: Generating Metadata Schema..."))
        
        yaml_prompt = (
            f"TASK: Generate the YAML frontmatter for a new lollms skill based on this request: '{prompt}'\n\n"
            "NAMING CONSTRAINTS:\n"
            "1. The 'name' field must describe the FUNCTION of the tool (e.g., 'code-humanizer', 'image-describer').\n"
            "2. STRIP all action verbs used in the request. Do NOT include 'build', 'create', 'make', or 'generate' in the name.\n"
            "3. Use kebab-case (lowercase with dashes).\n\n"
            "MANDATORY FIELDS:\n"
            "- name: (the identifier)\n"
            "- description: (what it does)\n"
            "- author: (ParisNeo)\n"
            "- version: 1.0.0\n"
            "- category: (the domain)\n\n"
            "STRICT: Output ONLY the raw YAML block. No talk. No markdown backticks. Zero preamble."
        )
        
        real_model, yaml_msgs = await _resolve_target(db, target_agent, [{"role": "user", "content": yaml_prompt}])
        
        if real_model == "__result__":
            raw_yaml = yaml_msgs[-1]["content"] if yaml_msgs else ""
        else:
            servers = await server_crud.get_servers_with_model(db, real_model)
            if not servers: return JSONResponse({"error": "Backend offline"}, status_code=503)

            y_resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": yaml_msgs, "stream": False}).encode(), is_subrequest=True, request_id=f"{build_id}_y", model=real_model, sender=admin_user.username)
            raw_yaml = json.loads(y_resp.body.decode()).get("message", {}).get("content", "").strip()
        # Clean potential markdown fences from lazy models
        raw_yaml = raw_yaml.replace("```yaml", "").replace("```", "").strip()
        
        # Parse for filename safety
        meta_parsed = SkillsManager.parse_frontmatter(f"---\n{raw_yaml}\n---")
        skill_id = meta_parsed.get("name", f"skill_{secrets.token_hex(4)}")
        filename = f"{skill_id}.md".replace(" ", "-").lower()

        # --- PHASE 2: GENERATE MARKDOWN CONTENT ---
        event_manager.emit(ProxyEvent("active", build_id, "Skill Builder", target_agent, admin_user.username, error_message=f"Step 2/2: Writing logic for '{skill_id}'..."))
        
        content_prompt = (
            f"TASK: Write the Markdown content body for the skill: '{skill_id}'.\n"
            f"METADATA CONTEXT: {raw_yaml}\n"
            f"USER REQUIREMENT: {prompt}\n"
            f"REFERENCE FILES: {file_context}\n\n"
            "INSTRUCTIONS:\n"
            "1. Start with a # Title.\n"
            "2. Provide clear ## Instructions or ## Workflow.\n"
            "3. Use professional markdown.\n"
            "STRICT: Output ONLY the markdown body. No YAML, no preamble, no chatter."
        )
        
        real_model_b, body_msgs = await _resolve_target(db, target_agent, [{"role": "user", "content": content_prompt}])
        
        if real_model_b == "__result__":
            markdown_body = body_msgs[-1]["content"] if body_msgs else ""
        else:
            b_resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model_b, "messages": body_msgs, "stream": False}).encode(), is_subrequest=True, request_id=f"{build_id}_b", model=real_model_b, sender=admin_user.username)
            markdown_body = json.loads(b_resp.body.decode()).get("message", {}).get("content", "").strip()

        # --- FINAL ASSEMBLY ---
        final_content = f"---\n{raw_yaml}\n---\n\n{markdown_body}"
        SkillsManager.save_skill(filename, final_content)
        
        event_manager.emit(ProxyEvent("completed", build_id, "Skill Builder", "Local", admin_user.username, error_message="Deployment Successful!"))
        return {"success": True, "filename": filename, "content": final_content}
    except Exception as e:
        logger.error(f"Skill build failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/skills/edit", name="api_edit_skill")
async def api_edit_skill(
    request: Request,
    prompt: str = Form(...),
    current_content: str = Form(...),
    csrf_token: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Refines an existing skill using the Management Agent."""
    from app.api.v1.dependencies import get_csrf_token
    import secrets

    stored_token = await get_csrf_token(request)
    if not stored_token or not secrets.compare_digest(csrf_token, stored_token):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")

    app_settings = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    if not target_agent:
        return JSONResponse({"error": "No Management Agent set in Settings."}, status_code=503)

    file_context = ""
    valid_files = [f for f in files if f and f.filename] if files else []
    if valid_files:
        file_context = await kit.extract_local_file_content(valid_files)

    instruction = (
        "You are a Senior LoLLMs Skill Architect. Your task is to EDIT an existing skill based on user instructions and provided context.\n\n"
        "STRICT CONSTRAINTS:\n"
        "1. Maintain the YAML frontmatter structure.\n"
        "2. Improve or modify the markdown logic as requested.\n"
        "3. Output ONLY the raw .md content (YAML block + Markdown body).\n"
        "4. No chat, no explanations, no markdown code fences around the entire output."
    )

    user_payload = (
        f"USER INSTRUCTIONS:\n{prompt}\n\n"
        f"CURRENT SKILL CONTENT:\n{current_content}\n\n"
    )
    if file_context:
        user_payload += f"EXTERNAL CONTEXT/REFERENCES:\n{file_context}"

    try:
        req_id = f"sys_edit_skill_{secrets.token_hex(4)}"
        real_model, msgs = await _resolve_target(db, target_agent, [{"role": "user", "content": user_payload}])
        msgs.insert(0, {"role": "system", "content": instruction})
        
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Backend nodes offline"}, status_code=503)

        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False}).encode(), is_subrequest=True, request_id=req_id, model=real_model, sender=admin_user.username)
        
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            edited_code = re.sub(r'^```markdown\s*|```$', '', data.get("message", {}).get("content", ""), flags=re.MULTILINE).strip()
            return {"success": True, "edited_content": edited_code}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/skills/search", name="api_search_external")
async def api_search_external(
    request_data: SearchRequest,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Searches external providers and returns snippets for selection."""
    pm.ensure_packages(["wikipedia", "arxiv", "youtube-transcript-api", "scrapemaster"], verbose=True)
    
    results = []
    q = request_data.query

    try:
        if request_data.provider == 'wikipedia':
            import wikipedia
            search_results = wikipedia.search(q)
            for title in search_results[:5]:
                try:
                    page = wikipedia.page(title, auto_suggest=False)
                    results.append({"title": page.title, "snippet": page.summary[:300] + "...", "url": page.url, "content": page.content})
                except: continue

        elif request_data.provider == 'arxiv':
            import arxiv
            import httpx
            pm.ensure_packages(["pypdf"], verbose=True)
            import pypdf
            
            client = arxiv.Client()
            search = arxiv.Search(query=q, max_results=5, sort_by=arxiv.SortCriterion.Relevance)
            for res in client.results(search):
                content = f"Title: {res.title}\nAuthors: {res.authors}\nAbstract: {res.summary}"
                
                if request_data.full_content:
                    try:
                        # Attempt to download and parse the PDF
                        response = httpx.get(res.pdf_url, follow_redirects=True)
                        pdf_file = io.BytesIO(response.content)
                        reader = pypdf.PdfReader(pdf_file)
                        pdf_text = "\n".join([page.extract_text() for page in reader.pages])
                        content += f"\n\n--- FULL PAPER CONTENT ---\n{pdf_text}"
                    except Exception as e:
                        content += f"\n\n[Error extracting full text: {str(e)}]"

                results.append({
                    "title": res.title, 
                    "snippet": res.summary[:300] + "...", 
                    "url": res.pdf_url, 
                    "content": content
                })

        elif request_data.provider == 'google':
            settings = await get_settings(request_data) # This might need manual lookup if dependency inject is weird in sub-route
            # Using a free alternative if no key, or SerpApi if key exists
            if settings.google_search_api_key:
                # Mocking SerpApi logic - you'd use 'google-search-results' pkg
                results.append({"title": "Google Search requires 'google-search-results' package", "snippet": "SerpApi integration active."})
            else:
                pm.ensure_packages(["googlesearch-python"])
                from googlesearch import search
                for url in search(q, num_results=5):
                    results.append({"title": url, "snippet": "Web Result", "url": url, "is_url": True})

        elif request_data.provider == 'youtube':
            from youtube_transcript_api import YouTubeTranscriptApi
            # Extract ID
            video_id = q.split("v=")[-1].split("&")[0] if "v=" in q else q
            try:
                transcript = YouTubeTranscriptApi.get_transcript(video_id)
                text = " ".join([t['text'] for t in transcript])
                results.append({"title": f"YouTube Transcript: {video_id}", "snippet": text[:300] + "...", "content": text})
            except Exception as e:
                return JSONResponse({"error": f"Could not fetch transcript: {str(e)}"}, status_code=400)

        return results
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/skills/scrape", name="api_scrape_url")
async def api_scrape_url(url: str = Form(...), depth: int = Form(1), admin_user: User = Depends(require_admin_user)):
    pm.ensure_packages(["scrapemaster"])
    from scrapemaster import WebScraper
    
    scraper = WebScraper(respect_robots_txt=True)
    try:
        # Simple extraction using scrape-master logic
        data = scraper.scrape_url(url, {'content': 'body::text', 'title': 'title::text'})
        return {"title": data.get('title'), "content": data.get('content')}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/skills/import", name="api_import_skill", dependencies=[Depends(validate_csrf_token)])
async def api_import_skill(
    request: Request,
    file: UploadFile = File(...),
    admin_user: User = Depends(require_admin_user)
):
    """Imports a skill from a .md file or a .skill (zip) archive."""
    try:
        content_bytes = await file.read()
        
        if file.filename.endswith(".md"):
            # Raw markdown import
            content = content_bytes.decode('utf-8')
            saved_name = SkillsManager.save_skill(file.filename, content)
            return {"success": True, "filename": saved_name}
            
        elif file.filename.endswith(".skill") or file.filename.endswith(".zip"):
            # Archive import
            saved_name = SkillsManager.import_skill_zip(content_bytes)
            return {"success": True, "filename": saved_name}
            
        else:
            return JSONResponse({"error": "Unsupported file format. Must be .md or .skill"}, status_code=400)
            
    except Exception as e:
        logger.error(f"Error importing skill: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
