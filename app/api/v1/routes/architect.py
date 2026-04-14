from fastapi import APIRouter, Depends, Request, Form, File, UploadFile
from fastapi.responses import JSONResponse
from app.core.architect_manager import MasterArchitect
from app.api.v1.routes.admin import require_admin_user
from app.core import knowledge_importer as kit
from app.database.session import get_db

router = APIRouter()

@router.post("/architect/execute", name="admin_architect_execute")
async def architect_execute(
    request: Request,
    objective: str = Form(...),
    prompt: str = Form(...),
    csrf_token: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    db = Depends(get_db),
    admin_user = Depends(require_admin_user)
):
    # Validate CSRF
    from app.api.v1.dependencies import get_csrf_token
    import secrets
    stored = await get_csrf_token(request)
    if not stored or not secrets.compare_digest(csrf_token, stored):
        return JSONResponse({"error": "CSRF mismatch"}, status_code=403)

    # Standardized File Context
    file_context = ""
    valid_files = [f for f in files if f.filename]
    if valid_files:
        file_context = await kit.extract_local_file_content(valid_files)

    try:
        result = await MasterArchitect.execute_build(db, request, objective, prompt, file_context, admin_user.username)
        
        # Standardized Persistence Logic based on objective
        if objective == "tool":
            from app.core.tools_manager import ToolsManager
            ToolsManager.save_tool(result["filename"], result["content"])
        elif objective == "node":
            from app.api.v1.routes.node_builder import save_custom_node_internal
            await save_custom_node_internal(result["filename"], result["py_code"], result["js_code"])
        # ... other logic maps here ...
        
        return {"success": True, "data": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)