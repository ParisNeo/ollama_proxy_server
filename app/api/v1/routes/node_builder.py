import json
from pathlib import Path
from fastapi import APIRouter, Request, Depends
from app.api.v1.routes.admin import require_admin_user

router = APIRouter()

async def save_custom_node_internal(filename: str, class_name: str, py_code: str):
    """Utility to save nodes from both manual and AI flows."""
    if not filename.endswith(".py"): filename += ".py"
    custom_dir = Path("app/nodes/custom")
    custom_dir.mkdir(parents=True, exist_ok=True)
    target_file = custom_dir / filename
    target_file.write_text(py_code, encoding="utf-8")
    return True

@router.post("/node-builder/save", name="admin_api_save_custom_node")
async def save_custom_node(request: Request, admin_user=Depends(require_admin_user)):
    # ... handle manual json data or AI sculptor data ...
    try:
        data = await request.json()
        await save_custom_node_internal(data['name'], data['class_name'], data['py_code'])
        return {"success": True, "message": "Node deployed successfully."}
    except Exception as e:
        return {"success": False, "error": str(e)}