import secrets
import logging
import json
from typing import List, Optional, Any
from fastapi import APIRouter, Depends, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.database.models import User
from app.api.v1.dependencies import get_csrf_token, validate_csrf_token
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.core.tools_manager import ToolsManager
from app.core import knowledge_importer as kit
from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
from app.crud import server_crud
import re
import os
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
    # FIX: Normalize line endings to prevent \r\n doubling and strip trailing bloat
    clean_content = content.replace('\r\n', '\n').strip() + '\n'
    saved = ToolsManager.save_tool(filename, clean_content)
    # Return metadata so the UI can update the title immediately
    meta = ToolsManager.parse_metadata(clean_content)
    return {"success": True, "filename": saved, "meta": meta}

@router.delete("/api/tools/{filename}", name="api_delete_tool")
async def api_delete_tool(filename: str, request: Request, admin_user: User = Depends(require_admin_user)):
    from app.api.v1.dependencies import get_csrf_token
    token = request.headers.get("X-CSRF-Token")
    stored = await get_csrf_token(request)
    if not token or not stored or not secrets.compare_digest(token, stored):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")
    if ToolsManager.delete_tool(filename):
        return {"success": True}
    return JSONResponse({"error": "File not found"}, status_code=404)

@router.post("/api/tools/rename", name="api_rename_tool")
async def api_rename_tool(
    request: Request,
    old_filename: str = Form(...),
    new_filename: str = Form(...),
    admin_user: User = Depends(require_admin_user)
):
    """Renames a tool library file on disk."""
    import os
    from app.core.tools_manager import USER_TOOLS_DIR, SYSTEM_TOOLS_DIR
    
    safe_old = re.sub(r'[^\w\-\.]', '', old_filename)
    safe_new = re.sub(r'[^\w\-\.]', '', new_filename)
    if not safe_new.endswith(".py"): safe_new += ".py"

    # Check User dir first, then System dir
    old_path = USER_TOOLS_DIR / safe_old
    if not old_path.exists():
        old_path = SYSTEM_TOOLS_DIR / safe_old

    new_path = USER_TOOLS_DIR / safe_new # Always rename into USER space

    if not old_path.exists():
        raise HTTPException(status_code=404, detail="Original file not found")

    try:
        os.rename(str(old_path.absolute()), str(new_path.absolute()))
        return {"success": True, "new_filename": safe_new}
    except Exception as e:
        logger.error(f"Rename failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/tools/init", name="api_init_tool")
async def api_init_tool(filename: str = Form(...), admin_user: User = Depends(require_admin_user)):
    """Dynamically loads the tool library and executes its init_tool_library function."""
    import importlib.util
    from app.core.tools_manager import USER_TOOLS_DIR, SYSTEM_TOOLS_DIR
    
    path = USER_TOOLS_DIR / filename
    if not path.exists(): path = SYSTEM_TOOLS_DIR / filename
    
    try:
        spec = importlib.util.spec_from_file_location("dynamic_tool_lib", str(path.absolute()))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        if hasattr(module, "init_tool_library"):
            await run_in_threadpool(module.init_tool_library)
            return {"success": True, "message": "Dependencies initialized successfully."}
        return {"success": True, "message": "No initialization function found. Library is ready."}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def _execute_tool_call_local(code: str, call: dict) -> Any:
    """Helper to execute a function from a raw string using provided arguments."""
    import importlib.util
    import sys
    import tempfile

    fn_name = call.get("function", {}).get("name")
    args = call.get("function", {}).get("arguments", {})
    if isinstance(args, str):
        try: args = json.loads(args)
        except: pass

    # 1. Write to temp file to import as module
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode='w', encoding='utf-8') as f:
        f.write(code)
        temp_path = f.name

    try:
        spec = importlib.util.spec_from_file_location("test_tool_lib", temp_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        if hasattr(module, fn_name):
            func = getattr(module, fn_name)
            # Execute in threadpool to prevent blocking the event loop
            return await run_in_threadpool(func, args)
        return f"Error: Function {fn_name} not found in code."
    except Exception as e:
        import traceback
        return f"CRASH DURING EXECUTION:\n{str(e)}\n\nTRACEBACK:\n{traceback.format_exc()}"
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)

@router.post("/api/tools/test", name="api_test_tool")
async def api_test_tool(
    request: Request,
    filename: str = Form(...),
    user_prompt: str = Form(...),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Asks AI to call a tool, then EXECUTES that tool locally and returns results."""
    from app.core.tools_manager import USER_TOOLS_DIR, SYSTEM_TOOLS_DIR, ToolsManager
    path = USER_TOOLS_DIR / filename
    if not path.exists(): path = SYSTEM_TOOLS_DIR / filename
    
    content = path.read_text(encoding="utf-8")
    tool_defs = ToolsManager.get_tool_definitions(content)
    
    if not tool_defs:
        return JSONResponse({"error": "No valid tool definitions found."}, status_code=400)

    app_settings = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    if not target_agent: return JSONResponse({"error": "No Management Agent set."}, status_code=503)

    try:
        # PHASE 1: Get Tool Call from AI
        real_model, msgs = await _resolve_target(db, target_agent, [{"role": "user", "content": user_prompt}])
        servers = await server_crud.get_servers_with_model(db, real_model)
        
        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "tools": tool_defs, "stream": False}).encode(), is_subrequest=True)
        
        if not hasattr(resp, 'body'): return JSONResponse({"error": "Empty response from AI"}, status_code=500)
        
        ai_data = json.loads(resp.body.decode())
        ai_msg = ai_data.get("message", {})
        tool_calls = ai_msg.get("tool_calls", [])

        # PHASE 2: Execute found tool calls
        execution_results = []
        if tool_calls:
            for call in tool_calls:
                result = await _execute_tool_call_local(content, call)
                execution_results.append({
                    "call": call,
                    "output": result
                })

        return {
            "success": True, 
            "ai_response": ai_msg,
            "executions": execution_results
        }
    except Exception as e:
        logger.error(f"Test Execution Failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/tools/fix", name="api_fix_tool")
async def api_fix_tool(
    request: Request,
    filename: str = Form(...),
    error_log: str = Form(...),
    prompt: str = Form(""),
    csrf_token: str = Form(...),
    files: Optional[List[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Uses Hub Agent to fix the tool code based on error output and attached references."""
    from app.core.tools_manager import USER_TOOLS_DIR, SYSTEM_TOOLS_DIR
    path = USER_TOOLS_DIR / filename
    if not path.exists(): path = SYSTEM_TOOLS_DIR / filename
    
    current_code = path.read_text(encoding="utf-8")
    app_settings = request.app.state.settings
    target_agent = app_settings.admin_agent_name

    file_context = ""
    valid_files = [f for f in files if f and f.filename] if files else []
    if valid_files:
        file_context = await kit.extract_local_file_content(valid_files)

    instruction = (
        "You are a Senior AI Debugger. Review the tool implementation, the console output/error, and user instructions. "
        "Rewrite the code to fix the bugs or implement the requested features. "
        "Output ONLY the updated raw Python code for the entire file. Start with the library variables. No chat."
    )

    user_payload = (
        f"CONSOLE OUTPUT / ERRORS:\n{error_log}\n\n"
        f"DEVELOPER INSTRUCTIONS:\n{prompt}\n\n"
    )
    if file_context:
        user_payload += f"REFERENCE DOCUMENTATION:\n{file_context}\n\n"
    
    user_payload += f"CURRENT SOURCE CODE:\n{current_code}"

    try:
        real_model, msgs = await _resolve_target(db, target_agent, [{"role": "user", "content": user_payload}])
        msgs.insert(0, {"role": "system", "content": instruction})
        
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Backend nodes offline"}, status_code=503)

        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False}).encode(), is_subrequest=True)
        
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            fixed_code = re.sub(r'^```python\s*|```$', '', data.get("message", {}).get("content", ""), flags=re.MULTILINE).strip()
            # Normalize line endings
            fixed_code = fixed_code.replace('\r\n', '\n').strip() + '\n'
            return {"success": True, "fixed_code": fixed_code}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

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
        "    # pm.ensure_packages({'package_name':'the version == or >= or <= etc..'})\n\n"
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
        
        # Normalize code from AI
        clean_code = raw_code.replace('\r\n', '\n').strip() + '\n'
        meta = ToolsManager.parse_metadata(clean_code)
        
        # EXTRACT NAME FROM VARIABLE: 
        # Convert 'Wikipedia Search' -> 'wikipedia_search.py'
        lib_id = meta.get('name', 'new_toolset').lower().replace(' ', '_')
        filename = f"{lib_id}.py"
        
        ToolsManager.save_tool(filename, clean_code)
        
        event_manager.emit(ProxyEvent("completed", build_id, "Tool Architect", "Local", admin_user.username, error_message="Deployment Successful!"))
        return {"success": True, "filename": filename, "content": raw_code}
    except Exception as e:
        logger.error(f"Tool Build Failed: {e}")
        event_manager.emit(ProxyEvent("error", build_id, "Tool Architect", "Local", admin_user.username, error_message=str(e)))
        return JSONResponse({"error": str(e)}, status_code=500)