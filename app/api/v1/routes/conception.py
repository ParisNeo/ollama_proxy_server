import json
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_csrf_token, validate_csrf_token_header
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.crud import server_crud
from app.database.models import User, Workflow, DataStore, VirtualAgent, SmartRouter, EnsembleOrchestrator
from app.database.session import get_db
from app.nodes.registry import NodeRegistry

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
