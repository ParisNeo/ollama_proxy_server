import secrets
import logging
import json
from typing import List, Optional
from fastapi import APIRouter, Depends, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.database.models import User
from app.api.v1.dependencies import get_csrf_token, validate_csrf_token
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.core.tools_manager import ToolsManager
from app.core import knowledge_importer as kit
from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
from app.crud import server_crud

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/tools", response_class=HTMLResponse, name="admin_tools")
async def admin_tools_page(request: Request, admin_user=Depends(require_admin_user)):
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/tools.html", context)

@router.get("/api/tools", name="api_get_tools")
async def api_get_tools(admin_user=Depends(require_admin_user)):
    return ToolsManager.get_all_tools()

@router.post("/api/tools", name="api_save_tool", dependencies=[Depends(validate_csrf_token)])
async def api_save_tool(filename: str = Form(...), content: str = Form(...)):
    saved = ToolsManager.save_tool(filename, content)
    return {"success": True, "filename": saved}

@router.delete("/api/tools/{filename}", name="api_delete_tool")
async def api_delete_tool(filename: str, request: Request, admin_user=Depends(require_admin_user)):
    from app.api.v1.dependencies import get_csrf_token
    token = request.headers.get("X-CSRF-Token")
    if not token or not secrets.compare_digest(token, await get_csrf_token(request)):
        raise HTTPException(status_code=403)
    if ToolsManager.delete_tool(filename):
        return {"success": True}
    return JSONResponse({"error": "Not found"}, status_code=404)

@router.post("/api/tools/build", name="api_build_tool")
async def api_build_tool(
    request: Request,
    prompt: str = Form(...),
    csrf_token: str = Form(...),
    files: List[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db),
    admin_user = Depends(require_admin_user)
):
    from app.core.events import event_manager, ProxyEvent
    build_id = f"sys_build_tool_{secrets.token_hex(4)}"
    app_settings = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    
    if not target_agent:
        return JSONResponse({"error": "No Management Agent set in Settings."}, status_code=503)

    event_manager.emit(ProxyEvent("received", build_id, "Tool Architect", "Local", admin_user.username, error_message="Initializing Tool Architect..."))

    file_context = ""
    valid_files = [f for f in files if f and f.filename]
    if valid_files:
        file_context = await kit.extract_local_file_content(valid_files)

    strict_instruction = (
        "You are an AI Software Engineer. You output raw Python code for a LoLLMs Tool Library.\n\n"
        "MANDATORY FILE STRUCTURE:\n"
        "TOOL_LIBRARY_NAME = '...'\n"
        "TOOL_LIBRARY_DESC = '...'\n"
        "TOOL_LIBRARY_ICON = '...'\n\n"
        "def init_tool_library() -> None:\n"
        "    '''Initialize dependencies using pipmaster'''\n"
        "    import pipmaster as pm\n"
        "    # pm.ensure('...')\n\n"
        "def tool_[name](args):\n"
        "    '''Docstrings must include Args and Returns sections'''\n\n"
        "STRICT RULES:\n"
        "1. Output raw code ONLY. No markdown fences.\n"
        "2. No chat or explanations.\n"
        "3. Ensure all functions are prefixed with 'tool_'.\n"
        "4. Use error handling (try/except) inside tools."
    )

    user_request = f"TASK: Build a tool library for: {prompt}"
    if file_context: user_request += f"\n\nSOURCE CONTEXT:\n{file_context}"

    try:
        event_manager.emit(ProxyEvent("active", build_id, "Tool Architect", target_agent, admin_user.username, error_message="Generating implementation..."))
        
        real_model, msgs = await _resolve_target(db, target_agent, [{"role": "user", "content": user_request}])
        msgs.insert(0, {"role": "system", "content": strict_instruction})
        
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Backend offline"}, status_code=503)

        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False}).encode(), is_subrequest=True, request_id=build_id, model=real_model, sender=admin_user.username)
        
        raw_code = json.loads(resp.body.decode()).get("message", {}).get("content", "").strip()
        # Clean markdown fences
        raw_code = re.sub(r'^```python\s*|```$', '', raw_code, flags=re.MULTILINE).strip()
        
        meta = ToolsManager.parse_metadata(raw_code)
        filename = f"{meta['name'].lower().replace(' ', '_')}.py"
        ToolsManager.save_tool(filename, raw_code)
        
        event_manager.emit(ProxyEvent("completed", build_id, "Tool Architect", "Local", admin_user.username, error_message="Deployment Successful!"))
        return {"success": True, "filename": filename, "content": raw_code}
    except Exception as e:
        logger.error(f"Tool Build Failed: {e}")
        event_manager.emit(ProxyEvent("error", build_id, "Tool Architect", "Local", admin_user.username, error_message=str(e)))
        return JSONResponse({"error": str(e)}, status_code=500)