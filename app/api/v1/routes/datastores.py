import os
import json
import secrets
import logging
import asyncio
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException, BackgroundTasks
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
import re

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
        import sqlite3
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        
        v_key = VECTORIZER_MAP.get(v_name, v_name)
        s = SafeStore(db_path=db_path, vectorizer_name=v_key, vectorizer_config=v_config)
        
        # 1. Resilient Path Signature Mapping
        # We normalize all paths to forward-slashes and identify files by their 
        # last two path segments to prevent collisions and handle absolute/relative mismatches.
        def get_sig(p):
            if not p: return ""
            parts = p.replace('\\', '/').lower().strip('/').split('/')
            # Use last two parts (e.g. 'folder/file.md') or just filename if root
            return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]

        counts_map = {}
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT document_title, COUNT(*) FROM chunks GROUP BY document_title")
            for row in cursor.fetchall():
                sig = get_sig(row[0])
                counts_map[sig] = counts_map.get(sig, 0) + row[1]
            conn.close()
        except Exception as e:
            logger.warning(f"SQL Count error: {e}")

        # 2. Map counts back to the SafeStore registry
        with s:
            docs = s.list_documents() if hasattr(s, "list_documents") else s.get_documents()
            if not docs: return []

            result = []
            for d in docs:
                path = d if isinstance(d, str) else d.get('file_path')
                sig = get_sig(path)

                # Check for match in our signature map
                real_count = counts_map.get(sig, 0)

                result.append({
                    "file_path": path,
                    "chunk_count": real_count
                })
            return result

    try:
        docs = await run_in_threadpool(_initialize_and_list)
        return {"success": True, "documents": docs}
    except Exception as e:
        logger.error(f"Boot error for store {ds.name}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

async def _get_file_ai_metadata(request: Request, db: AsyncSession, content: str, agent_name: str, sender: str) -> str:
    """Uses the management agent to generate a summary for cohesion."""
    from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy

    # Improved Prompt for better AI adherence
    prompt = (
        "TASK: Provide a ultra-concise, one-sentence high-level summary of this document excerpt. "
        "Goal: This summary will act as a 'cohesion anchor' for semantic search. "
        "STRICT: Output only the summary. Do not use 'Here is a summary' or 'This document is...'.\n\n"
        f"EXCERPT:\n{content[:3000]}"
    )

    try:
        req_id = f"sys_ds_meta_{secrets.token_hex(4)}"
        logger.info(f"AI Metadata: Requesting summary via agent '{agent_name}'...")

        resolution = await _resolve_target(db, agent_name, [{"role": "user", "content": prompt}], request_id=req_id, sender=sender)
        real_model, final_msgs = resolution

        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: 
            logger.warning(f"AI Metadata: No compute nodes available for {real_model}")
            return ""

        # Increase timeout for complex document analysis
        resp, _ = await _reverse_proxy(
            request, "chat", servers, 
            json.dumps({"model": real_model, "messages": final_msgs, "stream": False}).encode(), 
            is_subrequest=True, request_id=req_id, model=real_model, sender=sender
        )

        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            summary = data.get("message", {}).get("content", "").strip()
            if summary:
                logger.info(f"AI Metadata: Successfully generated summary: {summary[:50]}...")
                return summary
    except Exception as e:
        logger.error(f"AI Metadata Generation Failed: {e}")
    return ""

async def _ingest_document_logic(request, db, ds, file_path, use_ai, admin_user, is_upload=False, upload_id_override=None):
    """Internal shared logic for file ingestion with AI metadata support."""
    from app.core.events import event_manager, ProxyEvent

    task_id = f"sys_ds_{ds.id}"
    # Normalize ID to use Forward Slashes only for DB consistency
    # This prevents Windows backslashes from breaking the signature mapping
    doc_identifier = (upload_id_override if upload_id_override else os.path.basename(file_path)).replace('\\', '/')

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
        pca_file = Path(ds.db_path).with_suffix(".pca.json")
        if pca_file.exists():
            try: os.remove(pca_file)
            except: pass

        import pipmaster as pm
        import sqlite3
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore

        try:
            s = SafeStore(
                db_path=ds.db_path,
                vectorizer_name=v_key,
                vectorizer_config=ds.vectorizer_config or {},
                chunking_strategy=ds.chunking_strategy,
                chunk_size=ds.chunk_size,
                chunk_overlap=ds.chunk_overlap
            )

            with s:
                if extracted_text is not None or ai_prefix:
                    full_text = ai_prefix + (extracted_text or "")
                    s.add_text(unique_id=doc_identifier, text=full_text, force_reindex=True)
                else:
                    s.add_document(file_path, force_reindex=True)

            # VERIFICATION: Check if chunks were actually created
            conn = sqlite3.connect(ds.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM chunks WHERE document_title = ?", (doc_identifier,))
            count = cursor.fetchone()[0]
            conn.close()

            if count == 0:
                err = f"Failed to generate semantic fragments for '{fname}'. The file might be empty or vectorization failed."
                logger.error(err)
                event_manager.emit(ProxyEvent("error", task_id, "Datastore", "Local", admin_user.username, error_message=err))
            else:
                logger.info(f"Successfully indexed {count} chunks for {doc_identifier}")

        except Exception as e:
            err_msg = f"SafeStore Ingestion Error: {str(e)}"
            logger.error(err_msg)
            event_manager.emit(ProxyEvent("error", task_id, "Datastore", "Local", admin_user.username, error_message=err_msg))
            raise

    await run_in_threadpool(_sync_store_op)
            
    event_manager.emit(ProxyEvent("completed", task_id, "Datastore", "Local", admin_user.username, error_message=f"Finished indexing: {fname}"))

@router.post("/datastores/{ds_id}/upload", name="admin_upload_datastore")
async def admin_upload_datastore(
    ds_id: int, 
    request: Request, 
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...), 
    use_ai_metadata: bool = Form(False),
    extensions: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """
    Asynchronous batch upload. 
    Saves files to a temporary staging area and delegates processing to a background task.
    """
    # CSRF Check for AJAX
    from app.api.v1.dependencies import validate_csrf_token_header
    if not await validate_csrf_token_header(request, request.headers.get("X-CSRF-Token")):
         raise HTTPException(status_code=403, detail="CSRF mismatch")

    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)

    batch_id = f"upload_batch_{secrets.token_hex(4)}"
    staging_dir = UPLOADS_TEMP_DIR / batch_id
    staging_dir.mkdir(parents=True, exist_ok=True)

    allowed_exts = [e.strip().lower() for e in extensions.split(",")] if extensions else []
    staged_files = []

    # 1. Physical Upload Phase (Fast) - Preserving Relative Paths
    for file in files:
        if not file.filename: continue

        ext = "." + file.filename.split(".")[-1].lower()
        if allowed_exts and ext not in allowed_exts:
            continue

        # SECURITY: Sanitize the relative path to prevent traversal
        # We strip leading slashes and '..' sequences
        clean_rel_path = re.sub(r'\.\.[\\/]', '', file.filename.lstrip('/\\'))
        temp_path = staging_dir / clean_rel_path

        # Create subdirectories if they exist in the upload
        temp_path.parent.mkdir(parents=True, exist_ok=True)

        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)

        staged_files.append(str(temp_path))

    if not staged_files:
        return JSONResponse({"success": False, "error": "No valid files found in the batch after filtering."}, status_code=400)

    # 2. Background Processing Task
    async def process_batch_upload():
        from app.database.session import AsyncSessionLocal
        from app.core.events import event_manager, ProxyEvent

        total = len(staged_files)
        event_manager.emit(ProxyEvent("received", batch_id, ds.name, "Local", admin_user.username, error_message=f"Starting batch ingestion ({total} files)..."))

        async with AsyncSessionLocal() as bg_db:
            bg_ds = await bg_db.get(DataStore, ds_id)
            for idx, file_path in enumerate(staged_files):
                # Calculate the original relative path for the ID (e.g. 'folder/file.md')
                # rather than just the basename.
                rel_id = os.path.relpath(file_path, str(staging_dir))

                progress = int(((idx + 1) / total) * 100)

                event_manager.emit(ProxyEvent(
                    "active", batch_id, ds.name, "Local", admin_user.username, 
                    error_message=f"Indexing ({idx+1}/{total}): {rel_id}",
                    token_count=progress
                ))

                try:
                    # Pass rel_id as the override for is_upload mode
                    await _ingest_document_logic(request, bg_db, bg_ds, file_path, use_ai_metadata, admin_user, is_upload=True, upload_id_override=rel_id)
                except Exception as e:
                    logger.error(f"Batch Item Failed: {fname} - {e}")
                finally:
                    if os.path.exists(file_path): os.remove(file_path)

        # Cleanup staging dir
        try: shutil.rmtree(str(staging_dir))
        except: pass

        event_manager.emit(ProxyEvent("completed", batch_id, ds.name, "Local", admin_user.username, error_message=f"Batch processed: {total} items indexed successfully.", token_count=100))

    background_tasks.add_task(process_batch_upload)

    return {
        "success": True, 
        "batch_id": batch_id, 
        "file_count": len(staged_files),
        "message": f"Successfully uploaded {len(staged_files)} files. Ingestion is running in background."
    }


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

@router.get("/datastores/{ds_id}/map_data", name="admin_get_datastore_map")
async def admin_get_datastore_map(ds_id: int, force: str = "0", db: AsyncSession = Depends(get_db)):
    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    pca_file = Path(ds.db_path).with_suffix(".pca.json")

    def _fetch_map():
        # --- CACHE REPAIR LOGIC ---
        if pca_file.exists():
            should_purge = (force == "1")
            try:
                with open(pca_file, "r") as f:
                    cached_data = json.load(f)
                    # If any point is missing text, the entire cache is corrupted/stale
                    if not cached_data or (len(cached_data) > 0 and not cached_data[0].get('chunk_text')):
                        logger.warning(f"Map Cache for {ds.name} is missing text. Purging.")
                        should_purge = True

                    if not should_purge:
                        return cached_data
            except: 
                should_purge = True

            if should_purge:
                try: os.remove(pca_file)
                except: pass
        
        import pipmaster as pm
        import sqlite3
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        v_key = VECTORIZER_MAP.get(ds.vectorizer_name, ds.vectorizer_name)
        s = SafeStore(db_path=ds.db_path, vectorizer_name=v_key, vectorizer_config=ds.vectorizer_config or {})
        
        points = []
        try:
            with s:
                # 1. Get raw PCA coordinates
                all_points = s.export_point_cloud(output_format='dict')
                if not all_points: return []

                # 2. Direct DB Fetch to get the text and tags
                # We fetch by ID to ensure perfect sequence alignment with SafeStore's internal ordering
                conn = sqlite3.connect(ds.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT chunk_text, metadata FROM chunks ORDER BY id ASC")
                db_rows = cursor.fetchall()
                conn.close()

                # 3. Synchronized Merge
                # We use the length of the coordinate set as the master loop
                for i, p in enumerate(all_points):
                    text_content = ""
                    tags = []

                    if i < len(db_rows):
                        # Safely extract text from the row tuple
                        text_content = str(db_rows[i][0]) if db_rows[i][0] else ""
                        try:
                            # Safely parse JSON metadata
                            meta_json = db_rows[i][1]
                            meta = json.loads(meta_json) if meta_json else {}
                            tags = meta.get("tags", [])
                        except:
                            pass

                    # Only add if we have valid coordinates AND text
                    # If text is missing, we use a placeholder to avoid empty points
                    points.append({
                        "x": float(p['x']),
                        "y": float(p['y']),
                        "document_title": str(p.get('document_title', 'Unknown')),
                        "chunk_text": text_content or "[No Text Recovered]",
                        "tags": tags
                    })
                    
            if points:
                with open(pca_file, "w") as f:
                    json.dump(points, f)
        except Exception as e:
            logger.error(f"Error generating semantic map: {e}")
            
        return points

    data = await run_in_threadpool(_fetch_map)
    return {"points": data}

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
        pca_file = Path(ds.db_path).with_suffix(".pca.json")
        if pca_file.exists():
            try: os.remove(pca_file)
            except: pass

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

@router.post("/datastores/{ds_id}/batch_delete", name="admin_batch_delete_docs")
async def admin_batch_delete_docs(
    ds_id: int, 
    request: Request, 
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Bulk deletion of indexed files."""
    from app.api.v1.dependencies import validate_csrf_token_header
    if not await validate_csrf_token_header(request, request.headers.get("X-CSRF-Token")):
         return JSONResponse({"error": "CSRF mismatch"}, status_code=403)

    data = await request.json()
    file_paths = data.get("file_paths", [])
    if not file_paths: return {"success": True, "count": 0}

    ds = await db.get(DataStore, ds_id)
    if not ds: raise HTTPException(status_code=404)

    def _bulk_delete():
        # Clear PCA cache
        pca_file = Path(ds.db_path).with_suffix(".pca.json")
        if pca_file.exists():
            try: os.remove(pca_file)
            except: pass

        from safe_store import SafeStore
        s = SafeStore(db_path=ds.db_path, vectorizer_name=ds.vectorizer_name, vectorizer_config=ds.vectorizer_config or {})
        with s:
            for path in file_paths:
                try: s.delete_document_by_path(path)
                except: pass
        return len(file_paths)

    count = await run_in_threadpool(_bulk_delete)
    return {"success": True, "count": count}

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
            # 1. Reconstruct text using official API
            full_text = s.reconstruct_document_text(file_path)

            if not full_text:
                # Fallback: SafeStore might be using just the basename as ID
                basename = os.path.basename(file_path)
                full_text = s.reconstruct_document_text(basename)
                if not full_text:
                    return {"error": f"Document content not found for identifier: {file_path}"}
            
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
