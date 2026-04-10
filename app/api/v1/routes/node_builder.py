import json
from pathlib import Path
from fastapi import APIRouter, Request, Depends
from app.api.v1.routes.admin import require_admin_user

router = APIRouter()
NODES_DIR = Path("app/custom_nodes")

@router.post("/node-builder/save")
async def save_custom_node(request: Request, admin_user=Depends(require_admin_user)):
    data = await request.json()
    name = data['name'].replace(' ', '_').lower()
    
    # Save JSON definition
    (NODES_DIR / f"{name}.json").write_text(json.dumps(data['definition']))
    # Save Python logic
    (NODES_DIR / f"{name}.py").write_text(data['logic'])
    
    return {"success": True}