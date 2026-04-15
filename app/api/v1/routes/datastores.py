import os
import json
import secrets
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi.concurrency import run_in_threadpool

from app.database.session import get_db
from app.database.models import User, DataStore
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates, flash
from app.api.v1.dependencies import validate_csrf_token
from app.core.events import event_manager, ProxyEvent
from ascii_colors import trace_exception

logger = logging.getLogger(__name__)
router = APIRouter()

# Mapping UI names to SafeStore internal keys
# FIXED: UI uses 'sentense_transformer' but we ensure internal 'st' mapping is robust.
VECTORIZER_MAP = {
    "sentense_transformer": "st",
    "tf_idf": "tfidf",
    "ollama": "ollama",
    "openai": "openai",
    "cohere": "cohere",
    "lollms": "lollms"
}

DATASTORES_DIR = Path("app/static/datastores")
UPLOADS_TEMP_DIR = Path("app/static/uploads/temp")
DATASTORE_ASSETS_DIR = Path("app/static/uploads/datastore_assets")

@router.get("/datastores", response_class=HTMLResponse, name="admin_datastores")
async def admin_datastores(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.api.v1.dependencies import get_csrf_token
    from app.crud import server_crud
    context = get_template_context(request)
    res = await db.execute(select(DataStore))
    context["datastores"] = res.scalars().all()
    # Provide models for the vectorizer config panel
    context["embed_models"] = await server_crud.get_all_available_model_names(db, filter_type='embedding')
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/datastores.html", context)

@router.post("/datastores/add", name="admin_add_datastore", dependencies=[Depends(validate_csrf_token)])
async def admin_add_datastore(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    vectorizer_name: str = Form("tf_idf"),
    vectorizer_model: str = Form(None),
    vectorizer_model_custom: str = Form(None),
    vectorizer_base_url: str = Form(None),
    vectorizer_api_key: str = Form(None),
    chunking_strategy: str = Form("recursive"),
    chunk_size: int = Form(512),
    chunk_overlap: int = Form(50),
    db: AsyncSession = Depends(get_db)
):
    DATASTORES_DIR.mkdir(parents=True, exist_ok=True)
    
    # Check duplicate
    existing = await db.execute(select(DataStore).filter(DataStore.name == name))
    if existing.scalars().first():
        flash(request, f"Datastore '{name}' already exists.", "error")
        return RedirectResponse(url=request.url_for("admin_datastores"), status_code=303)
        
    db_path = str(DATASTORES_DIR / f"{secrets.token_hex(8)}.db")
    
    # Internal Mapping to SafeStore keys
    v_key = vectorizer_name
    if v_key == "sentense_transformer": v_key = "st"
    elif v_key == "tf_idf": v_key = "tfidf"

    from app.core.config import settings
    v_config = {}
    final_model = vectorizer_model_custom.strip() if vectorizer_model_custom and vectorizer_model_custom.strip() else vectorizer_model
    if final_model: v_config["model"] = final_model
    
    # --- URL & HOST MAPPING ---
    # SafeStore uses 'host' for Ollama and 'base_url' for others.
    
    # Special handling for OpenAI vectorizer: if no URL is provided, default to Hub Proxy
    if vectorizer_name == "openai" and not vectorizer_base_url:
        target_url = f"http://127.0.0.1:{settings.OPENAI_PROXY_PORT}"
        logger.info(f"Datastore '{name}': OpenAI vectorizer selected with no URL. Defaulting to Hub Proxy.")
    else:
        target_url = vectorizer_base_url.strip() if vectorizer_base_url else f"http://127.0.0.1:{settings.PROXY_PORT}"
    
    if vectorizer_name == "ollama":
        v_config["host"] = target_url
    else:
        v_config["base_url"] = target_url
    
    # --- INTERNAL KEY MANAGEMENT ---
    if vectorizer_api_key:
        v_config["api_key"] = vectorizer_api_key
    else:
        # If pointing to self (localhost/127.0.0.1) or explicitly OpenAI without key, inject the Hub System Key
        url_str = target_url.lower()
        is_local = "localhost" in url_str or "127.0.0.1" in url_str
        
        if is_local or vectorizer_name == "openai":
            # Attempt to get system key, fallback gracefully if not available
            system_key = getattr(request.app.state, 'system_key', None)
            if system_key:
                v_config["api_key"] = system_key
                logger.info(f"Datastore '{name}': Injecting Hub System Key for local/OpenAI proxy usage.")
            else:
                logger.warning(f"Datastore '{name}': OpenAI vectorizer selected but system_key not found in app state. Request may fail without an API key.")
    
    ds = DataStore(
        name=name,
        description=description,
        db_path=db_path,
        vectorizer_name=vectorizer_name, # Keep UI name for persistence
        chunking_strategy=chunking_strategy,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        vectorizer_config=v_config
    )
    db.add(ds)
    await db.commit()
    
    def _init_store():
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        s = SafeStore(db_path=db_path, vectorizer_name=v_key, vectorizer_config=v_config)
    
    try:
        await run_in_threadpool(_init_store)
        flash(request, f"Datastore '{name}' created.", "success")
    except Exception as e:
        logger.error(f"Failed to initialize datastore: {e}")
        flash(request, f"Failed to initialize SafeStore: {e}", "error")
        
    return RedirectResponse(url=request.url_for("admin_datastores"), status_code=303)

@router.post("/datastores/{ds_id}/delete", name="admin_delete_datastore", dependencies=[Depends(validate_csrf_token)])
async def admin_delete_datastore(ds_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    ds = await db.get(DataStore, ds_id)
    if ds:
        try:
            if os.path.exists(ds.db_path):
                os.remove(ds.db_path)
        except Exception as e:
            logger.error(f"Failed to delete datastore file: {e}")
        await db.delete(ds)
        await db.commit()
        flash(request, "Datastore deleted.", "success")
    return RedirectResponse(url=request.url_for("admin_datastores"), status_code=303)

@router.get("/datastores/{ds_id}/manage", response_class=HTMLResponse, name="admin_manage_datastore")
async def admin_manage_datastore(ds_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    """Fast route: Returns the UI skeleton immediately."""
    from app.api.v1.dependencies import get_csrf_token
    ds = await db.get(DataStore, ds_id)
    if not ds:
        flash(request, "Datastore not found.", "error")
        return RedirectResponse(url=request.url_for("admin_datastores"), status_code=303)

    context = get_template_context(request)
    context["datastore"] = ds
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/manage_datastore.html", context)

@router.post("/datastores/{ds_id}/boot", name="admin_boot_datastore")
async def admin_boot_datastore(ds_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    """Slow route: Handles SafeStore initialization and returns document list."""
    ds = await db.get(DataStore, ds_id)
    if not ds: return JSONResponse({"error": "Not found"}, status_code=404)

    def _initialize_and_list():
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        
        v_key = VECTORIZER_MAP.get(ds.vectorizer_name, "tfidf")
        # Loading the store here triggers vectorizer load if needed
        s = SafeStore(
            db_path=ds.db_path, 
            vectorizer_name=v_key, 
            vectorizer_config=ds.vectorizer_config or {}
        )
        
        with s:
            docs = s.list_documents() if hasattr(s, "list_documents") else s.get_documents()
            # Normalize list of strings to objects
            if docs and isinstance(docs[0], str):
                return [{"file_path": d, "chunk_count": "N/A"} for d in docs]
            return docs

    try:
        docs = await run_in_threadpool(_initialize_and_list)
        return {"success": True, "documents": docs}
    except Exception as e:
        logger.error(f"Boot error for store {ds.name}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

async def _get_file_ai_metadata(request: Request, db: AsyncSession, content: str, agent_name: str, sender: str) -> str:
    """Uses the management agent to generate a summary for cohesion."""
    from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
    
    prompt = (
        "Analyze this first part of a document. Provide a very concise one-sentence contextual summary. "
        "This summary will be added to every chunk to maintain cohesion during RAG retrieval. Output ONLY the summary text.\n\n"
        f"CONTENT:\n{content[:2000]}"
    )
    
    try:
        req_id = f"sys_ds_meta_{secrets.token_hex(4)}"
        # Resolve to physical model
        resolution = await _resolve_target(db, agent_name, [{"role": "user", "content": prompt}], request_id=req_id, sender=sender)
        real_model, final_msgs = resolution
        
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return ""
        
        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": final_msgs, "stream": False}).encode(), is_subrequest=True, request_id=req_id, model=real_model, sender=sender)
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            return data.get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.warning(f"AI Metadata generation skipped: {e}")
    return ""

async def _ingest_document_logic(request, db, ds, file_path, use_ai, admin_user):
    """Internal shared logic for file ingestion with AI metadata support."""
    import pipmaster as pm
    from app.core.events import event_manager, ProxyEvent
    from safe_store import SafeStore
    
    task_id = f"sys_ds_{ds.id}" # Matches the frontend EventSource listener
    fname = os.path.basename(file_path)
    
    v_key = VECTORIZER_MAP.get(ds.vectorizer_name, "st")
    event_manager.emit(ProxyEvent("active", task_id, "Datastore", "Local", admin_user.username, error_message=f"Processing {fname}..."))

    # Load content for AI metadata
    ai_prefix = ""
    if use_ai:
        agent = request.app.state.settings.admin_agent_name
        if agent:
            event_manager.emit(ProxyEvent("active", task_id, "Datastore", "Local", admin_user.username, error_message=f"Generating cohesion summary for {fname} via {agent}..."))
            try:
                # Basic reading for preview
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    preview = f.read(4000)
                
                meta = await _get_file_ai_metadata(request, db, preview, agent, admin_user.username)
                if meta: 
                    ai_prefix = f"[Context: {meta.replace('[', '').replace(']', '')}]\n\n"
                    logger.info(f"Generated AI Cohesion Metadata for {fname}: {meta}")
            except Exception as e:
                logger.error(f"Failed to generate metadata for {fname}: {e}")

    def _sync_store_op():
        # Initialize store
        s = SafeStore(
            db_path=ds.db_path,
            vectorizer_name=v_key,
            vectorizer_config=ds.vectorizer_config or {},
            chunking_strategy=ds.chunking_strategy,
            chunk_size=ds.chunk_size,
            chunk_overlap=ds.chunk_overlap
        )
        
        with s:
            if ai_prefix:
                # Prepend context to the text so every chunk inherits it
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    full_text = ai_prefix + f.read()
                s.add_document_from_text(full_text, fname, force_reindex=True)
            else:
                s.add_document(file_path, force_reindex=True)
                
    await run_in_threadpool(_sync_store_op)
            
    event_manager.emit(ProxyEvent("completed", task_id, "Datastore", "Local", admin_user.username, error_message=f"Finished indexing: {fname}"))

@router.post("/datastores/{ds_id}/upload", name="admin_upload_datastore", dependencies=[Depends(validate_csrf_token)])
async def admin_upload_datastore(
    ds_id: int, request: Request, 
    file: UploadFile = File(...), 
    use_ai_metadata: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    UPLOADS_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = UPLOADS_TEMP_DIR / file.filename
    content = await file.read()
    with open(temp_path, "wb") as f:
        f.write(content)
            
    try:
        await _ingest_document_logic(request, db, ds, str(temp_path), use_ai_metadata, admin_user)
        flash(request, f"File '{file.filename}' indexed.", "success")
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        flash(request, f"Error: {e}", "error")
    finally:
        if temp_path.exists(): os.remove(temp_path)
            
    return RedirectResponse(url=request.url_for("admin_manage_datastore", ds_id=ds.id), status_code=303)


@router.post("/datastores/{ds_id}/add_folder", name="admin_add_folder_datastore", dependencies=[Depends(validate_csrf_token)])
async def admin_add_folder_datastore(
    ds_id: int, request: Request, 
    folder_path: str = Form(...),
    extensions: str = Form(".txt,.md,.pdf"),
    use_ai_metadata: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    p = Path(folder_path)
    if not p.exists() or not p.is_dir():
        flash(request, "Folder does not exist or is not a directory.", "error")
        return RedirectResponse(url=request.url_for("admin_manage_datastore", ds_id=ds.id), status_code=303)

    ext_list = [e.strip().lower() for e in extensions.split(",") if e.strip()]
    
    async def _folder_task():
        files_to_process = []
        for root, _, files in os.walk(folder_path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in ext_list):
                    files_to_process.append(os.path.join(root, f))
        
        # We need a fresh DB session for the background task to avoid Connection leaks
        from app.database.session import AsyncSessionLocal
        
        async with AsyncSessionLocal() as background_db:
            # Re-fetch the datastore in the new session to prevent detached instance errors
            background_ds = await background_db.get(DataStore, ds_id)
            if not background_ds: return
            
            for f_path in files_to_process:
                # _ingest_document_logic is already async and handles its own threadpooling internally now
                await _ingest_document_logic(request, background_db, background_ds, f_path, use_ai_metadata, admin_user)

    # Start background task wrapper
    asyncio.create_task(_folder_task())
    flash(request, "Background folder indexing task started. Check console for progress.", "info")
    return RedirectResponse(url=request.url_for("admin_manage_datastore", ds_id=ds.id), status_code=303)


@router.post("/datastores/{ds_id}/test_query", name="admin_test_query_datastore")
async def admin_test_query_datastore(
    ds_id: int, 
    query: str = Form(...),
    top_k: int = Form(5),
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user)
):
    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    def _query():
        from safe_store import SafeStore
        v_key = VECTORIZER_MAP.get(ds.vectorizer_name, ds.vectorizer_name)
        s = SafeStore(db_path=ds.db_path, vectorizer_name=v_key, vectorizer_config=ds.vectorizer_config or {})
        with s:
            return s.query(query, top_k=top_k)
            
    try:
        results = await run_in_threadpool(_query)
        # Format results for the UI
        formatted = []
        for r in results:
            formatted.append({
                "text": r.get("chunk_text"),
                "score": round(r.get("similarity", 0) * 100, 1),
                "source": r.get("document_title")
            })
        return {"results": formatted}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/datastores/{ds_id}/build_graph", name="admin_build_datastore_graph", dependencies=[Depends(validate_csrf_token)])
async def admin_build_datastore_graph(
    ds_id: int, 
    request: Request, 
    ontology_json: str = Form(...),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)

    # 1. Setup LLM Bridge
    app_settings = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    if not target_agent:
        flash(request, "No Management Agent set in Settings. Needed for graph extraction.", "error")
        return RedirectResponse(url=request.url_for("admin_manage_datastore", ds_id=ds.id), status_code=303)

    from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy

    async def hub_llm_callback(prompt: str) -> str:
        """Bridge between safe_store extraction prompts and Hub LLM nodes."""
        try:
            # Create a simple chat-style message list
            msgs =[{"role": "user", "content": prompt}]
            resolution = await _resolve_target(db, target_agent, msgs, request=request, sender="graph-builder")
            real_model, final_msgs = resolution
            
            servers = await server_crud.get_servers_with_model(db, real_model)
            if not servers: return "{}" # Failed to find compute

            # Execute via internal proxy
            resp, _ = await _reverse_proxy(
                request, "chat", servers, 
                json.dumps({"model": real_model, "messages": final_msgs, "stream": False}).encode(),
                is_subrequest=True, sender="graph-builder"
            )
            
            if hasattr(resp, 'body'):
                data = json.loads(resp.body.decode())
                return data.get("message", {}).get("content", "{}")
        except Exception as e:
            logger.error(f"Graph LLM Bridge error: {e}")
            if getattr(request.app.state.settings, "enable_debug_mode", False):
                trace_exception(e)
        return "{}"

    # 2. Start Extraction Task
    def _run_graph_build():
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore, GraphStore
        
        v_key = VECTORIZER_MAP.get(ds.vectorizer_name, "tfidf")
        s = SafeStore(db_path=ds.db_path, vectorizer_name=v_key, vectorizer_config=ds.vectorizer_config or {})
        
        try:
            ontology = json.loads(ontology_json)
        except:
            ontology = None # Fallback to default

        # Create GraphStore
        # We use a sync wrapper for the async callback since GraphStore is sync-heavy
        sync_callback = lambda p: asyncio.run(hub_llm_callback(p))
        
        graph = GraphStore(store=s, llm_executor_callback=sync_callback, ontology=ontology)
        
        task_id = f"sys_ds_graph_{ds.id}"
        event_manager.emit(ProxyEvent("active", task_id, "Graph Engine", "Local", admin_user.username, error_message="Scanning documents for entities..."))
        
        # Build the graph
        graph.build_graph_for_all_documents()
        
        event_manager.emit(ProxyEvent("completed", task_id, "Graph Engine", "Local", admin_user.username, error_message="Knowledge Graph build complete!"))

    asyncio.create_task(run_in_threadpool(_run_graph_build))
    flash(request, "Graph extraction started in background. Monitor the Live Flow or wait for the tab to refresh.", "info")
    return RedirectResponse(url=request.url_for("admin_manage_datastore", ds_id=ds.id), status_code=303)

@router.get("/datastores/{ds_id}/graph_data", name="admin_get_datastore_graph")
async def admin_get_datastore_graph(ds_id: int, db: AsyncSession = Depends(get_db)):
    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    def _fetch_data():
        import sqlite3
        conn = sqlite3.connect(ds.db_path)
        c = conn.cursor()
        
        # safe_store standard graph tables
        try:
            c.execute("SELECT id, label, type, properties FROM nodes")
            nodes = [{"id": r[0], "label": r[1], "type": r[2], "properties": json.loads(r[3])} for r in c.fetchall()]
            
            c.execute("SELECT source_id, target_id, type FROM relationships")
            edges = [{"from": r[0], "to": r[1], "label": r[2]} for r in c.fetchall()]
            
            conn.close()
            return {"nodes": nodes, "edges": edges}
        except:
            conn.close()
            return {"nodes": [], "edges": []}

    data = await run_in_threadpool(_fetch_data)
    return data

@router.post("/datastores/{ds_id}/delete_doc", name="admin_delete_datastore_doc", dependencies=[Depends(validate_csrf_token)])
async def admin_delete_datastore_doc(ds_id: int, request: Request, file_path: str = Form(...), db: AsyncSession = Depends(get_db)):
    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    def _del_doc():
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        s = SafeStore(db_path=ds.db_path, vectorizer_name=ds.vectorizer_name, vectorizer_config=ds.vectorizer_config or {})
        with s:
            if hasattr(s, "remove_document"): s.remove_document(file_path)
            elif hasattr(s, "delete_document"): s.delete_document(file_path)
            else: raise Exception("Deletion not supported by installed safe-store version")
                
    try:
        await run_in_threadpool(_del_doc)
        flash(request, "Document removed from datastore.", "success")
    except Exception as e:
        logger.error(f"Delete doc error: {e}")
        flash(request, f"Error removing document: {e}", "error")
        
    return RedirectResponse(url=request.url_for("admin_manage_datastore", ds_id=ds.id), status_code=303)

@router.get("/datastores/{ds_id}/view_doc", name="admin_view_datastore_doc")
async def admin_view_datastore_doc(ds_id: int, file_path: str, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    def _read_doc():
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        v_key = VECTORIZER_MAP.get(ds.vectorizer_name, ds.vectorizer_name)
        s = SafeStore(db_path=ds.db_path, vectorizer_name=v_key, vectorizer_config=ds.vectorizer_config or {})
        with s:
            if hasattr(s, "reconstruct_document_text"):
                return s.reconstruct_document_text(file_path)
            return "Reconstruction not supported by this safe-store version."
                
    try:
        content = await run_in_threadpool(_read_doc)
        return {"content": content, "file_path": file_path}
    except Exception as e:
        logger.error(f"View doc error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
