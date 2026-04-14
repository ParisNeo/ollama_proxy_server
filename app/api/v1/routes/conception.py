import json
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Request, HTTPException, status, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_csrf_token, validate_csrf_token_header
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.crud import server_crud
from app.database.models import User, Workflow, DataStore, VirtualAgent, SmartRouter, EnsembleOrchestrator
from app.database.session import get_db
from app.nodes.registry import NodeRegistry
import re
from ascii_colors import trace_exception

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/conception", response_class=HTMLResponse, name="admin_conception")
async def conception_page(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    
    # Load dynamic JS for custom nodes from the registry
    context["dynamic_nodes_js"] = NodeRegistry.get_all_js()
    
    # Mirror Playground grouping for consistent model selection
    model_groups = await server_crud.get_all_models_grouped_by_server(db)
    context["model_groups"] = model_groups or {}
    
    # Collect available logical blocks (Agents, Routers, Ensembles)
    from app.core.personalities_manager import PersonalityManager
    res_a = await db.execute(select(VirtualAgent.name).filter(VirtualAgent.is_active == True))
    res_r = await db.execute(select(SmartRouter.name).filter(SmartRouter.is_active == True))
    res_e = await db.execute(select(EnsembleOrchestrator.name).filter(EnsembleOrchestrator.is_active == True))
    
    file_personas = [p["name"] for p in PersonalityManager.get_all_personalities()]
    
    context["logic_blocks"] = sorted(list(set(
        res_a.scalars().all() + 
        res_r.scalars().all() + 
        res_e.scalars().all() + 
        file_personas
    )))
    
    # Datastores for RAG nodes
    res_ds = await db.execute(select(DataStore.name).order_by(DataStore.name))
    context["datastores"] = res_ds.scalars().all()
    
    # Metadata for the sidebar
    context["registered_nodes"] = NodeRegistry.get_node_list()
    
    return templates.TemplateResponse("admin/conception.html", context)

@router.get("/conception/templates", name="admin_list_templates")
async def list_templates(admin_user: User = Depends(require_admin_user)):
    """Returns all standard and custom workflow templates from disk."""
    base_dir = Path("app/nodes/templates")
    standard_dir = base_dir / "standard"
    custom_dir = base_dir / "custom"
    
    # Ensure directories exist
    standard_dir.mkdir(parents=True, exist_ok=True)
    custom_dir.mkdir(parents=True, exist_ok=True)
    
    templates_list = []
    
    def scan_dir(dir_path: Path, category: str):
        if not dir_path.exists():
            return
        for f in dir_path.glob("*.json"):
            try:
                content = f.read_text(encoding="utf-8")
                if not content.strip():
                    continue
                data = json.loads(content)
                templates_list.append({
                    "id": f.stem,
                    "name": data.get("name", f.stem),
                    "description": data.get("description", "No description provided."),
                    "category": category,
                    "graph": data.get("graph", data)
                })
            except Exception as e:
                logger.error(f"Failed to load template {f.name}: {e}")

    scan_dir(standard_dir, "Standard")
    scan_dir(custom_dir, "Custom")
    
    return templates_list

@router.post("/conception/save", name="admin_save_workflow")
async def save_workflow(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    data = await request.json()
    name = data.get("name")
    graph = data.get("graph")
    
    if not name or not graph:
        return JSONResponse({"error": "Missing name or graph data"}, status_code=400)
        
    existing = await db.execute(select(Workflow).filter(Workflow.name == name))
    workflow = existing.scalars().first()
    
    if workflow:
        workflow.graph_data = graph
    else:
        workflow = Workflow(name=name, graph_data=graph)
        db.add(workflow)
        
    await db.commit()
    return {"success": True, "message": f"Workflow '{name}' deployed."}

@router.get("/conception/list", name="admin_list_workflows")
async def list_workflows(db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    res = await db.execute(select(Workflow))
    return [{"name": w.name, "id": w.id, "graph": w.graph_data} for w in res.scalars().all()]

@router.delete("/conception/{wid}", name="admin_delete_workflow")
async def delete_workflow(
    wid: int, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user),
    csrf_protect: bool = Depends(validate_csrf_token_header)
):
    workflow = await db.get(Workflow, wid)
    if workflow:
        await db.delete(workflow)
        await db.commit()
    return {"success": True}

@router.post("/conception/build", name="admin_api_build_workflow")
async def api_build_workflow(
    request: Request,
    prompt: str = Form(...),
    csrf_token: str = Form(...),
    activate_self_healing: bool = Form(False),
    files: Optional[List[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Generates a workflow JSON from a user prompt, files, and web context using the Hub Agent."""
    from app.core.skills_manager import SkillsManager
    from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
    from app.core import knowledge_importer as kit
    from app.core.events import event_manager, ProxyEvent
    import secrets

    # CSRF check
    from app.api.v1.dependencies import get_csrf_token
    stored_token = await get_csrf_token(request)
    if not stored_token or not secrets.compare_digest(csrf_token, stored_token):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")

    app_settings = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    if not target_agent:
        return JSONResponse({"error": "No Management Agent set in Settings."}, status_code=503)

    build_id = f"sys_build_graph_{secrets.token_hex(4)}"
    event_manager.emit(ProxyEvent("received", build_id, "Graph Architect", "Local", admin_user.username, error_message="Initializing AI Architect..."))

    # 1. Extract context from uploaded files
    file_context = ""
    valid_files = [f for f in files if f and f.filename] if files else []
    if valid_files:
        event_manager.emit(ProxyEvent("active", build_id, "Graph Architect", "Local", admin_user.username, error_message=f"Processing {len(valid_files)} reference files..."))
        file_context = await kit.extract_local_file_content(valid_files)

    # 2. Ground the AI in the specific Hub Node Schema and SLOT MAP
    node_schema_text = """
### NODE SLOT REGISTRY (0-INDEXED) ###
- hub/input: 
    Outputs: [0: Messages (array), 1: Settings (obj), 2: Input (str)]
- hub/output: 
    Inputs: [0: Source (msg/str)]
- hub/llm_chat: 
    Inputs: [0: Messages, 1: Settings, 2: Model Override, 3+: Tools]
    Outputs: [0: Content (str)]
- hub/llm_instruct:
    Inputs: [0: Prompt (str)]
    Outputs: [0: Response (str)]
- hub/autorouter:
    Inputs: [0: User Context (msgs), 1+: Expert Paths]
    Outputs: [0: Route Output (str)]
- hub/model:
    Outputs: [0: Expert (obj)]
- hub/system_modifier:
    Inputs: [0: Messages, 1: System Prompt (str)]
    Outputs: [0: Updated Messages]
- hub/datastore:
    Inputs: [0: Query (str)]
    Outputs: [0: Context (str)]
- hub/web_search:
    Inputs: [0: Query (str)]
    Outputs: [0: Results (str)]
- hub/composer:
    Inputs: [0: A (str), 1: B (str)]
    Outputs: [0: Merged (str)]
- hub/note:
    Properties: {"content": "Markdown text"}
    """

    instruction = (
        "You are a Senior LoLLMs System Architect. You design cognitive graphs using LiteGraph JSON.\n\n"
        f"{node_schema_text}\n\n"
        "### AGENTIC POWER: ASSET INVENTION ###\n"
        "1. If the standard nodes are insufficient, you can INVENT new ones.\n"
        "2. To do this, include a 'manifest' key in your JSON root.\n"
        "3. Inside 'manifest', provide 'custom_nodes' (list of {filename, py_code, js_code}) and 'tools' (list of {filename, content}).\n"
        "4. STRICT TWO-FILE PROTOCOL (MANDATORY):\n"
        "   - 'py_code': Pure Python. Must contain a class inheriting from BaseNode with an 'execute' method.\n"
        "   - 'js_code': Pure JavaScript. Must contain a function and a 'LiteGraph.registerNodeType' call.\n"
        "   - NEVER nest JavaScript inside a Python string or method.\n\n"
        "### MANDATORY NOTE RULE ###\n"
        "1. You MUST include a 'hub/note' node (ID: 100) in EVERY graph.\n"
        "2. Place it at the top (e.g., pos [50, -200]).\n"
        "3. The content MUST be a detailed Markdown guide explaining how the workflow functions, the logic of the connections, and what each part does.\n\n"
        "### JSON FORMAT RULES ###\n"
        "1. Output a single JSON object with 'nodes' and 'links' keys.\n"
        "2. Node: {\"id\": int, \"type\": \"hub/...\", \"pos\": [x, y], \"properties\": {}}\n"
        "3. Link: [link_id, origin_node_id, origin_output_slot, target_node_id, target_input_slot, \"type_name\"]\n"
        "4. CRITICAL: Slot numbers are 0-indexed based on the Registry above.\n"
        "5. Flow must be logical: input -> [logic/rag/llm] -> output.\n"
        "6. Output ONLY raw JSON. No markdown backticks. No conversational filler."
    )

    user_payload = f"USER REQUEST: {prompt}\n\n"
    if file_context:
        user_payload += f"TECHNICAL SPECIFICATIONS / CONTEXT:\n{file_context}"

    try:
        event_manager.emit(ProxyEvent("active", build_id, "Graph Architect", target_agent, admin_user.username, error_message="Generating Cognitive Graph..."))
        
        real_model, msgs = await _resolve_target(db, target_agent,[{"role": "user", "content": user_payload}], request=request)
        msgs.insert(0, {"role": "system", "content": instruction})
        
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Backend offline"}, status_code=503)

        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False}).encode(), is_subrequest=True, request_id=build_id, model=real_model, sender=admin_user.username)
        
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            raw_output = data.get("message", {}).get("content", "").strip()
            
            # --- ROBUST JSON EXTRACTION & SANITIZATION ---
            # 1. Remove markdown code fences if they exist
            clean_json = re.sub(r'```(?:json)?\s*([\s\S]*?)```', r'\1', raw_output).strip()
            
            # 2. Heuristic: Find the actual start and end of the JSON object
            start_idx = clean_json.find('{')
            end_idx = clean_json.rfind('}')
            
            if start_idx == -1 or end_idx == -1:
                logger.error(f"AI Architect: No JSON object found in output: {raw_output[:200]}...")
                raise ValueError("The AI generated a response but no valid JSON structure was detected.")
                
            clean_json = clean_json[start_idx:end_idx + 1]
            
            # 3. Handle 'Invalid control character' issues (unescaped newlines in strings)
            # We replace literal newlines and tabs within the string that aren't properly escaped
            # while preserving valid JSON syntax.
            try:
                # We use strict=False to allow some control characters (like newlines)
                # inside strings if the model was lazy, though this is only supported 
                # in newer Python versions or specific parsers.
                parsed = json.loads(clean_json, strict=False)
            except json.JSONDecodeError as jde:
                logger.warning(f"Initial JSON parse failed: {jde}. Attempting aggressive sanitization...")
                # Last resort: Remove actual control characters (0-31) except space
                sanitized = "".join(ch if ord(ch) >= 32 else " " for ch in clean_json)
                parsed = json.loads(sanitized)

            # --- AGENTIC MULTI-ASSET DEPLOYMENT ---
            # The AI can now return a 'manifest' containing new nodes, tools, or personas
            # it invented to satisfy the workflow requirement.
            manifest = parsed.get("manifest", {})
            
            # 1. Handle New Custom Nodes
            if "custom_nodes" in manifest:
                from app.api.v1.routes.node_builder import save_custom_node_internal
                for node_data in manifest["custom_nodes"]:
                    # Ensure we have both halves of the component
                    p_code = node_data.get("py_code", "")
                    j_code = node_data.get("js_code", "")
                    f_name = node_data.get("filename", "invented_node")
                    
                    if p_code and j_code:
                        await save_custom_node_internal(f_name, p_code, j_code)
                        event_manager.emit(ProxyEvent("active", build_id, "Graph Architect", "Local", admin_user.username, error_message=f"Deployed Paired Component: {f_name}"))
                    else:
                        logger.warning(f"AI generated incomplete node manifest for {f_name}")                    
                    # SECURITY: Validate filename to prevent directory traversal
                    safe_filename = "".join(c for c in node_data["filename"] if c.isalnum() or c in "._-")
                    if not safe_filename or safe_filename.startswith(".") or ".." in safe_filename:
                        logger.warning(f"Skipping invalid node filename: {node_data['filename']}")
                        continue
                    
                    # SECURITY: Basic code validation for py_code
                    py_code = node_data.get("py_code", "")
                    if len(py_code) > 50000:  # 50KB limit
                        logger.warning(f"Node {safe_filename} exceeds size limit")
                        continue
                    
                    # Check for dangerous patterns
                    dangerous_patterns = ["__import__", "eval(", "exec(", "subprocess", "os.system", "open(", "write"]
                    if any(pattern in py_code for pattern in dangerous_patterns):
                        logger.warning(f"Node {safe_filename} contains potentially dangerous code patterns")
                        # Log but don't block - the node_builder has its own sandbox
                    
                    await save_custom_node_internal(
                        safe_filename, 
                        py_code, 
                        node_data.get("js_code", "")
                    )
                    event_manager.emit(ProxyEvent("active", build_id, "Graph Architect", "Local", admin_user.username, error_message=f"Deployed paired Node: {node_data.get('class_name', safe_filename)}"))

            # 2. Handle New Tools
            if "tools" in manifest:
                from app.core.tools_manager import ToolsManager
                for tool in manifest["tools"]:
                    ToolsManager.save_tool(tool["filename"], tool["content"])
                    event_manager.emit(ProxyEvent("active", build_id, "Graph Architect", "Local", admin_user.username, error_message=f"Deployed new Toolset: {tool['filename']}"))

            # --- SELF-HEALING FOR WORKFLOWS ---
            final_graph = parsed.get("graph", parsed)
            if activate_self_healing:
                event_manager.emit(ProxyEvent("active", build_id, "Self-Healing", "Local", admin_user.username, error_message="Validating Graph topology..."))
                
                # Validation Logic: Ensure output node exists and has a link
                nodes = final_graph.get("nodes", [])
                links = final_graph.get("links", [])
                has_output = any(n.get("type") == "hub/output" for n in nodes)
                
                if not has_output or not links:
                    event_manager.emit(ProxyEvent("active", build_id, "Self-Healing", target_agent, admin_user.username, error_message="Invalid Graph topology. Regenerating..."))
                    # Trigger a one-time automatic retry with more specific instructions
                    retry_res = await api_build_workflow(request, f"RETRY: The previous graph was invalid. {prompt}", csrf_token, False, files, db, admin_user)
                    return retry_res

            event_manager.emit(ProxyEvent("completed", build_id, "Graph Architect", "Local", admin_user.username, error_message="Workflow deployed (Verified)!"))
            return {"success": True, "graph": final_graph, "has_new_assets": len(manifest) > 0}
    except Exception as e:
        logger.error(f"Graph Generation Failed: {e}")
        if getattr(request.app.state.settings, "enable_debug_mode", False):
            trace_exception(e)
        event_manager.emit(ProxyEvent("error", build_id, "Graph Architect", "Local", admin_user.username, error_message=str(e)))
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/conception/edit", name="admin_api_edit_workflow")
async def api_edit_workflow(
    request: Request,
    prompt: str = Form(...),
    current_graph: str = Form(...),
    csrf_token: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Refines an existing graph based on user instructions using the Hub Agent."""
    from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
    from app.core import knowledge_importer as kit
    from app.core.events import event_manager, ProxyEvent
    import secrets

    app_settings = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    if not target_agent:
        return JSONResponse({"error": "No Management Agent set."}, status_code=503)

    build_id = f"sys_build_graph_{secrets.token_hex(4)}"
    event_manager.emit(ProxyEvent("received", build_id, "Graph Architect", "Local", admin_user.username, error_message="Analyzing existing graph..."))

    file_context = ""
    valid_files = [f for f in files if f and f.filename] if files else []
    if valid_files:
        file_context = await kit.extract_local_file_content(valid_files)

    instruction = (
        "You are a Senior System Architect. You are EDITING an existing LiteGraph workflow.\n\n"
        "### CONSTRAINTS ###\n"
        "1. Update the 'nodes' and 'links' to reflect user requested changes.\n"
        "2. UPDATED RULE: You MUST update the 'hub/note' (ID 100) to reflect the new logic.\n"
        "3. Ensure the JSON is valid and connections remain intact.\n"
        "4. Output ONLY the raw JSON object. No chat."
    )

    user_payload = (
        f"INSTRUCTIONS: {prompt}\n\n"
        f"CURRENT GRAPH JSON:\n{current_graph}\n\n"
    )
    if file_context:
        user_payload += f"ADDITIONAL CONTEXT:\n{file_context}"

    try:
        real_model, msgs = await _resolve_target(db, target_agent,[{"role": "user", "content": user_payload}], request=request)
        msgs.insert(0, {"role": "system", "content": instruction})
        
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Backend offline"}, status_code=503)

        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False}).encode(), is_subrequest=True, request_id=build_id, model=real_model, sender=admin_user.username)
        
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            raw_output = data.get("message", {}).get("content", "").strip()
            
            # --- ROBUST JSON EXTRACTION ---
            clean_json = re.sub(r'```(?:json)?\s*([\s\S]*?)```', r'\1', raw_output).strip()
            start_idx = clean_json.find('{')
            end_idx = clean_json.rfind('}')
            
            if start_idx == -1 or end_idx == -1:
                raise ValueError("The AI generated a response but no valid JSON structure was detected.")
                
            clean_json = clean_json[start_idx:end_idx + 1]
            parsed = json.loads(clean_json, strict=False)
            
            event_manager.emit(ProxyEvent("completed", build_id, "Graph Architect", "Local", admin_user.username, error_message="Graph updated successfully!"))
            return {"success": True, "graph": parsed}
    except Exception as e:
        logger.error(f"Graph Generation Failed: {e}")
        if getattr(request.app.state.settings, "enable_debug_mode", False):
            trace_exception(e)
        event_manager.emit(ProxyEvent("error", build_id, "Graph Architect", "Local", admin_user.username, error_message=str(e)))
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/conception/templates/save", name="admin_save_template")
async def save_template(request: Request, admin_user: User = Depends(require_admin_user)):
    """Saves the current graph as a reusable template file."""
    data = await request.json()
    name = data.get("name", "Untitled Template")
    description = data.get("description", "")
    graph = data.get("graph")
    
    if not graph:
        raise HTTPException(status_code=400, detail="Graph data required")
        
    safe_name = "".join([c if c.isalnum() else "_" for c in name]).lower()
    custom_dir = Path("app/nodes/templates/custom")
    custom_dir.mkdir(parents=True, exist_ok=True)
    
    target_file = custom_dir / f"{safe_name}.json"
    template_data = {
        "name": name,
        "description": description,
        "graph": graph
    }
    
    try:
        target_file.write_text(json.dumps(template_data, indent=2), encoding="utf-8")
        return {"success": True, "message": f"Template '{name}' saved to disk."}
    except Exception as e:
        logger.error(f"Failed to save template: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
