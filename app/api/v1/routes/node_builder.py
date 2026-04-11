import json
from pathlib import Path
from fastapi import APIRouter, Request, Depends
from app.api.v1.routes.admin import require_admin_user

router = APIRouter()

@router.post("/node-builder/save", name="admin_api_save_custom_node")
async def save_custom_node(request: Request, admin_user=Depends(require_admin_user)):
    """
    Saves a unified Python node containing both JS frontend and Python backend logic.
    """
    data = await request.json()
    filename = data['name']
    class_name = data['class_name']
    py_code = data['py_code']
    
    # Validation: Basic check for required structure
    if "get_frontend_js" not in py_code or "BaseNode" not in py_code:
         return {"success": False, "error": "Node must inherit from BaseNode and implement get_frontend_js."}

    custom_dir = Path("app/nodes/custom")
    custom_dir.mkdir(parents=True, exist_ok=True)
    
    target_file = custom_dir / f"{filename}.py"
    try:
        target_file.write_text(py_code, encoding="utf-8")
        return {"success": True, "message": f"Node '{class_name}' deployed to {filename}.py. Refresh Workflow Architect to use it."}
    except Exception as e:
        return {"success": False, "error": f"IO Error: {str(e)}"}