import json
from pathlib import Path
from fastapi import APIRouter, Request, Depends
from app.api.v1.routes.admin import require_admin_user
import logging
logger =logging.getLogger("node_builder")
router = APIRouter()

async def save_custom_node_internal(filename: str, py_code: str, js_code: str):
    """Utility to save nodes into component-based folders."""
    base_name = filename.replace(".py", "").replace(".js", "").lower()
    
    # Create the specific node component folder
    component_dir = Path("app/nodes/custom") / base_name
    component_dir.mkdir(parents=True, exist_ok=True)
    
    # Write Python Logic
    py_path = component_dir / f"{base_name}.py"
    py_path.write_text(py_code, encoding="utf-8")
    
    # Write JS UI
    js_path = component_dir / f"{base_name}.js"
    js_path.write_text(js_code, encoding="utf-8")
    
    return True

@router.post("/node-builder/save", name="admin_api_save_custom_node")
async def save_custom_node(request: Request, admin_user=Depends(require_admin_user)):
    """
    Handles node deployment from Node Studio and AI Sculptor.
    Saves into component folders: app/nodes/custom/[name]/[name].py and .js
    """
    try:
        data = await request.json()
        # Validation
        if not data.get('py_code') or not data.get('js_code'):
            return JSONResponse({"success": False, "error": "Both Python logic and JavaScript UI are required for a node pair."}, status_code=400)
            
        await save_custom_node_internal(
            data['name'], 
            data['py_code'], 
            data['js_code']
        )
        return {"success": True, "message": f"Component folder '{data['name']}' deployed successfully."}
    except Exception as e:
        logger.error(f"Node Builder Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)