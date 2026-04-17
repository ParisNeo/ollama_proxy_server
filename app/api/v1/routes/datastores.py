import os
import json
import secrets
import logging
import asyncio
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi.concurrency import run_in_threadpool

from app.core import knowledge_importer as kit
from app.database.session import get_db
from app.database.models import User, DataStore
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates, flash
from app.api.v1.dependencies import validate_csrf_token
from app.core.events import event_manager, ProxyEvent
from ascii_colors import trace_exception
from typing import List
import asyncio


logger = logging.getLogger(__name__)
router = APIRouter()

# Mapping UI names to SafeStore internal keys
# FIXED: UI uses 'sentense_transformer' but we ensure internal 'st' mapping is robust.
# 'lollms' in the Hub UI uses the Hub proxy, which follows the 'ollama' protocol.
VECTORIZER_MAP = {
    "sentense_transformer": "st",
    "tf_idf": "tf_idf",
    "ollama": "ollama",
    "openai": "openai",
    "cohere": "cohere",
    "lollms": "ollama"
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
    elif v_key == "tf_idf": v_key = "tf_idf"

    from app.core.config import settings
    v_config = {}
    final_model = vectorizer_model_custom.strip() if vectorizer_model_custom and vectorizer_model_custom.strip() else vectorizer_model
    
    # safe-store's ollama/openai bindings require a model
    if not final_model and vectorizer_name in ("ollama", "lollms", "openai"):
        final_model = "nomic-embed-text" if vectorizer_name != "openai" else "text-embedding-3-small"
        
    if final_model: v_config["model"] = final_model
    
    # --- URL & HOST MAPPING ---
    app_settings = request.app.state.settings
    
    # Handle the 'lollms' proxy loopback separately to use the correct ports and protocol
    if vectorizer_name == "lollms":
        # lollms option uses the primary proxy port
        target_url = f"http://127.0.0.1:{settings.PROXY_PORT}"
        v_config["host"] = target_url
        logger.info(f"Datastore '{name}': lollms proxy loopback selected. URL: {target_url}")
    elif vectorizer_name == "openai" and not vectorizer_base_url:
        # openai option with no URL uses the secondary OpenAI port
        target_url = f"http://127.0.0.1:{app_settings.openai_port}"
        v_config["base_url"] = target_url
        logger.info(f"Datastore '{name}': OpenAI loopback selected. URL: {target_url}")
    else:
        # Remote or manual URL
        target_url = vectorizer_base_url.strip() if vectorizer_base_url else f"http://127.0.0.1:{settings.PROXY_PORT}"
        if vectorizer_name == "ollama":
            v_config["host"] = target_url
        else:
            v_config["base_url"] = target_url
    
    # --- INTERNAL KEY MANAGEMENT ---
    # --- INTERNAL KEY MANAGEMENT ---
    # We always prioritize the Hub's own SECRET_KEY for local loopbacks
    # to ensure SafeStore has bypass privileges for embeddings.
    url_str = target_url.lower()
    is_loopback = "localhost" in url_str or "127.0.0.1" in url_str
    
    if is_loopback:
        from app.core.config import settings as bootstrap_settings
        v_config["api_key"] = bootstrap_settings.SECRET_KEY.strip().strip('"').strip("'")
        logger.info(f"Datastore '{name}': Loopback detected. Injecting Hub SECRET_KEY for internal authentication.")
    elif vectorizer_api_key:
        v_config["api_key"] = vectorizer_api_key.strip().strip('"').strip("'")
    
    try:
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
    except Exception as e:
        import traceback
        err_trace = traceback.format_exc()
        logger.error(f"Failed to store updated vectorizer params: {e}\n{err_trace}")
        flash(request, "Failed to store updated vectorizer params", "error", trace=err_trace)
        return RedirectResponse(url=request.url_for("admin_datastores"), status_code=303)
    
    def _init_store():
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        # Ensure we open and close the store to verify parameters and release locks
        with SafeStore(db_path=db_path, vectorizer_name=v_key, vectorizer_config=v_config):
            pass
    
    try:
        await run_in_threadpool(_init_store)
        flash(request, f"Datastore '{name}' created.", "success")
    except Exception as e:
        import traceback
        err_trace = traceback.format_exc()
        logger.error(f"Failed to initialize datastore: {e}\n{err_trace}")
        flash(request, f"Failed to initialize SafeStore: {e}", "error", trace=err_trace)
        
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

    # Capture config before passing to thread
    db_path = ds.db_path
    v_name = ds.vectorizer_name
    v_config = ds.vectorizer_config or {}
    store_name = ds.name

    def _initialize_and_list():
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        
        v_key = VECTORIZER_MAP.get(v_name, v_name)
        logger.info(f"Booting SafeStore: {store_name} with binding: {v_key}")
        
        s = SafeStore(
            db_path=db_path, 
            vectorizer_name=v_key, 
            vectorizer_config=v_config
        )
        
        with s:
            docs = s.list_documents() if hasattr(s, "list_documents") else s.get_documents()
            if not docs: return []
            if isinstance(docs[0], str):
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

async def _ingest_document_logic(request, db, ds, file_path, use_ai, admin_user, is_upload=False):
    """Internal shared logic for file ingestion with AI metadata support."""
    from app.core.events import event_manager, ProxyEvent

    task_id = f"sys_ds_{ds.id}"
    fname = os.path.basename(file_path)

    v_key = VECTORIZER_MAP.get(ds.vectorizer_name, "st")
    event_manager.emit(ProxyEvent("active", task_id, "Datastore", "Local", admin_user.username, error_message=f"Processing {fname}..."))

    # 1. Extract text if necessary (Async)
    extracted_text = None
    if is_upload or use_ai or file_path.lower().endswith(('.pdf', '.docx')):
        class MockFile:
            def __init__(self, p): self.filename = p; self.content_type = "app/binary"
            async def read(self):
                with open(self.filename, "rb") as f: return f.read()

        try:
            extracted_text = await kit.extract_local_file_content([MockFile(file_path)])
        except Exception as e:
            logger.error(f"Extraction failed for {fname}: {e}")

    # 2. Generate AI summary (Async)
    ai_prefix = ""
    if use_ai and extracted_text:
        agent = request.app.state.settings.admin_agent_name
        if agent:
            event_manager.emit(ProxyEvent("active", task_id, "Datastore", "Local", admin_user.username, error_message=f"Generating cohesion summary for {fname} via {agent}..."))
            try:
                meta = await _get_file_ai_metadata(request, db, extracted_text[:4000], agent, admin_user.username)
                if meta: 
                    ai_prefix = f"[Context: {meta.replace('[', '').replace(']', '')}]\n\n"
            except Exception as e:
                logger.error(f"Failed to generate AI metadata for {fname}: {e}")

    # 3. Synchronous Indexing (Threadpool)
    def _sync_store_op():
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore

        s = SafeStore(
            db_path=ds.db_path,
            vectorizer_name=v_key,
            vectorizer_config=ds.vectorizer_config or {},
            chunking_strategy=ds.chunking_strategy,
            chunk_size=ds.chunk_size,
            chunk_overlap=ds.chunk_overlap
        )

        with s:
            # If we have extracted text or AI prefix, use text-based indexing (add_text)
            # to ensure the 'unique_id' is the filename, preventing "file not found" errors
            # after the temporary upload file is deleted.
            if extracted_text is not None or ai_prefix:
                full_text = ai_prefix + (extracted_text or "")
                # API check: unique_id is the first positional or kwarg 'unique_id'
                s.add_text(unique_id=fname, text=full_text, force_reindex=True)
            else:
                # Direct file path indexing for local folders
                s.add_document(file_path, force_reindex=True)

    await run_in_threadpool(_sync_store_op)
            
    event_manager.emit(ProxyEvent("completed", task_id, "Datastore", "Local", admin_user.username, error_message=f"Finished indexing: {fname}"))

@router.post("/datastores/{ds_id}/upload", name="admin_upload_datastore", dependencies=[Depends(validate_csrf_token)])
async def admin_upload_datastore(
    ds_id: int, request: Request, 
    files: List[UploadFile] = File(...), 
    use_ai_metadata: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    UPLOADS_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    
    # Process each file in the batch
    for file in files:
        if not file.filename: continue
        
        # Security: Clean filename to prevent path injection
        safe_name = os.path.basename(file.filename)
        temp_path = UPLOADS_TEMP_DIR / safe_name
        
        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)
                
        try:
            # Flag this as an upload so basename is used as ID
            await _ingest_document_logic(request, db, ds, str(temp_path), use_ai_metadata, admin_user, is_upload=True)
        except Exception as e:
            logger.error(f"Upload failed for {safe_name}: {e}")
            flash(request, f"Error indexing {safe_name}: {e}", "error")
        finally:
            if temp_path.exists(): os.remove(temp_path)
            
    flash(request, f"Batch processing of {len(files)} items complete.", "success")
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
        
        v_key = VECTORIZER_MAP.get(ds.vectorizer_name, "tf_idf")
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
            s.delete_document_by_path(file_path)
                
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
        from safe_store import SafeStore
        
        v_key = VECTORIZER_MAP.get(ds.vectorizer_name, ds.vectorizer_name)
        s = SafeStore(db_path=ds.db_path, vectorizer_name=v_key, vectorizer_config=ds.vectorizer_config or {})
        
        full_text = ""
        pca_points = []
        chunk_count = 0
        
        with s:
            # 1. Verify existence
            docs = s.list_documents()
            doc_found = False
            for d in docs:
                d_path = d['file_path'] if isinstance(d, dict) else d
                if d_path == file_path:
                    doc_found = True
                    break
            
            if not doc_found:
                return {"error": f"Document '{file_path}' not found in storage index."}

            # 2. Reconstruct text using official API
            full_text = s.reconstruct_document_text(file_path)
            
            # 3. Get visualization points using official export_point_cloud API
            # This method already performs PCA/Dimensionality reduction internally.
            all_points = s.export_point_cloud(output_format='dict')
            
            # Filter the global point cloud for chunks belonging to THIS document
            for p in all_points:
                # safe_store uses document_title as the key for filtering
                if p.get('document_title') == file_path:
                    chunk_count += 1
                    pca_points.append({
                        "x": p['x'],
                        "y": p['y'],
                        "label": f"Chunk #{chunk_count}",
                        "preview": p.get('chunk_text', '')[:100] + "..."
                    })

        return {
            "content": full_text,
            "file_path": file_path,
            "chunk_count": chunk_count,
            "pca_points": pca_points
        }
                
    try:
        result = await run_in_threadpool(_read_doc)
        return result
    except Exception as e:
        logger.error(f"View doc error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
