from fastapi import APIRouter, Depends, Request, HTTPException, status, Form
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import json

from app.database.session import get_db
from app.database.models import User, Workflow
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates, flash
from app.api.v1.dependencies import validate_csrf_token_header
from app.crud import server_crud
from pathlib import Path
router = APIRouter()

def load_nodes():
    """Dynamically loads node definitions from the nodes directory."""
    nodes = []
    base_dir = Path("app/nodes")
    # Scan both subdirectories
    for nodes_dir in [base_dir / "standard", base_dir / "custom"]:
        if not nodes_dir.exists(): continue
        for js_file in nodes_dir.glob("*.js"):
            nodes.append({"filename": js_file.name, "content": js_file.read_text()})
    return nodes

@router.get("/conception", response_class=HTMLResponse, name="admin_conception")
async def conception_page(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    
    # Mirror Playground grouping for consistent model selection (Workflows, Agents, Pools, etc.)
    model_groups = await server_crud.get_all_models_grouped_by_server(db)
    context["model_groups"] = model_groups or {}
    
    # We still keep logic_blocks for other UI elements if needed
    from app.database.models import VirtualAgent, SmartRouter, EnsembleOrchestrator
    res_a = await db.execute(select(VirtualAgent.name))
    res_r = await db.execute(select(SmartRouter.name))
    res_e = await db.execute(select(EnsembleOrchestrator.name))
    context["logic_blocks"] = res_a.scalars().all() + res_r.scalars().all() + res_e.scalars().all()
    
    context["nodes"] = load_nodes()
    return templates.TemplateResponse("admin/conception.html", context)

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
