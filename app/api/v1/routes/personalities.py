import secrets
import logging
import json
from typing import List, Optional
from fastapi import APIRouter, Depends, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.session import get_db
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.core.personalities_manager import PersonalityManager
from app.api.v1.dependencies import get_csrf_token, validate_csrf_token
from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
from app.core import knowledge_importer as kit
from app.core.skills_manager import SkillsManager
from app.crud import server_crud

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/personalities", response_class=HTMLResponse, name="admin_personalities")
async def admin_personalities_page(request: Request, admin_user=Depends(require_admin_user)):
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/personalities.html", context)

@router.get("/api/personalities", name="api_get_personalities")
async def api_get_personalities(admin_user=Depends(require_admin_user)):
    return PersonalityManager.get_all_personalities()

@router.post("/api/personalities", name="api_save_personality", dependencies=[Depends(require_admin_user)])
async def api_save_personality(filename: str = Form(...), content: str = Form(...)):
    saved = PersonalityManager.save_personality(filename, content)
    return {"success": True, "filename": saved}

@router.get("/api/personalities/{filename}/export", name="api_export_personality")
async def api_export_personality(filename: str, admin_user=Depends(require_admin_user)):
    zip_bytes = PersonalityManager.export_personality_zip(filename)
    return Response(content=zip_bytes, media_type="application/zip", headers={'Content-Disposition': f'attachment; filename="{filename.replace(".md", ".lps")}"'})

@router.post("/api/personalities/import", name="api_import_personality", dependencies=[Depends(require_admin_user)])
async def api_import_personality(file: UploadFile = File(...)):
    content_bytes = await file.read()
    saved = PersonalityManager.import_personality_zip(content_bytes)
    return {"success": True, "filename": saved}

@router.delete("/api/personalities/{filename}", name="api_delete_personality", dependencies=[Depends(require_admin_user)])
async def api_delete_personality(filename: str, request: Request):
    if PersonalityManager.delete_personality(filename):
        return {"success": True}
    return JSONResponse({"error": "Not found"}, status_code=404)

@router.post("/api/personalities/build", name="api_build_personality")
async def api_build_personality(
    request: Request,
    prompt: str = Form(...),
    csrf_token: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_db),
    admin_user = Depends(require_admin_user)
):
    """Generates a complete Lollms Personality (.md) via the Management Agent."""
    from app.core.events import event_manager, ProxyEvent
    build_id = f"sys_build_persona_{secrets.token_hex(4)}"
    
    app_settings = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    
    if not target_agent:
        return JSONResponse({"error": "No Management Agent set in Settings."}, status_code=503)

    # Use 'received' only once at the start
    event_manager.emit(ProxyEvent("received", build_id, "Persona Builder", "Local", admin_user.username, error_message="Initializing Persona Architect..."))

    file_context = ""
    if files and any(f.filename for f in files):
        file_context = await kit.extract_local_file_content([f for f in files if f.filename])

    try:
        # --- PHASE 1: GENERATE YAML METADATA ---
        event_manager.emit(ProxyEvent("active", build_id, "Persona Builder", target_agent, admin_user.username, error_message="Step 1/2: Structuring Metadata..."))
        
        yaml_prompt = (
            f"TASK: Generate the YAML frontmatter for a new lollms persona based on: '{prompt}'\n\n"
            "FIELDS:\n- name: (kebab-case identifier)\n- description: (triggers)\n- author: ParisNeo\n"
            "STRICT: Output raw YAML only. No backticks."
        )
        
        real_model, yaml_msgs = await _resolve_target(db, target_agent, [{"role": "user", "content": yaml_prompt}])
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Backend offline"}, status_code=503)

        # Internal proxy calls don't need manual emits; they track themselves.
        y_resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": yaml_msgs, "stream": False}).encode(), is_subrequest=True, request_id=f"{build_id}_y", model=real_model, sender=admin_user.username)
        raw_yaml = json.loads(y_resp.body.decode()).get("message", {}).get("content", "").strip()
        raw_yaml = raw_yaml.replace("```yaml", "").replace("```", "").strip()
        
        # Parse for consistency
        meta_parsed = SkillsManager.parse_frontmatter(f"---\n{raw_yaml}\n---")
        p_id = meta_parsed.get("name", f"persona_{secrets.token_hex(4)}")
        filename = f"{p_id}.md"

        # --- PHASE 2: GENERATE SOUL BODY ---
        event_manager.emit(ProxyEvent("active", build_id, "Persona Builder", target_agent, admin_user.username, error_message=f"Step 2/2: Sculpting Soul Logic for '{p_id}'..."))
        
        content_prompt = (
            f"TASK: Write the Markdown 'Identity' and 'Behaviour' sections for persona: '{p_id}'.\n"
            f"METADATA: {raw_yaml}\nUSER REQUEST: {prompt}\nCONTEXT: {file_context}\n\n"
            "STRICT: Output markdown body only. No YAML. No preamble."
        )
        
        _, body_msgs = await _resolve_target(db, target_agent, [{"role": "user", "content": content_prompt}])
        b_resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": body_msgs, "stream": False}).encode(), is_subrequest=True, request_id=f"{build_id}_b", model=real_model, sender=admin_user.username)
        markdown_body = json.loads(b_resp.body.decode()).get("message", {}).get("content", "").strip()

        # --- ASSEMBLY ---
        full_content = f"---\n{raw_yaml}\n---\n\n{markdown_body}"
        PersonalityManager.save_personality(filename, full_content)
        
        event_manager.emit(ProxyEvent("completed", build_id, "Persona Builder", "Local", admin_user.username, error_message="Deployment Successful!"))
        return {"success": True, "filename": filename, "content": full_content}

    except Exception as e:
        logger.error(f"Persona Build Failed: {e}")
        event_manager.emit(ProxyEvent("error", build_id, "Persona Builder", "Local", admin_user.username, error_message=str(e)))
        return JSONResponse({"error": str(e)}, status_code=500)
        p_id = meta.get("name", f"persona_{secrets.token_hex(4)}")
        filename = f"{p_id}.md"

        PersonalityManager.save_personality(filename, full_content)
        
        event_manager.emit(ProxyEvent("completed", build_id, "Persona Builder", "Local", admin_user.username, error_message="Persona Deployed Successfully."))
        return {"success": True, "filename": filename, "content": full_content}
    except Exception as e:
        logger.error(f"Persona Build Failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)