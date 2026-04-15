import json
import logging
import random
import asyncio
import secrets
import re
import numpy as np 
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, Request, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.concurrency import run_in_threadpool

from app.database.session import get_db, AsyncSessionLocal
from app.database.models import User, DataStore, BenchmarkDataset, BenchmarkRun
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.core.config import settings as bootstrap_settings
from app.api.v1.dependencies import validate_csrf_token, get_csrf_token
from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
from app.crud import server_crud
from app.core.events import event_manager, ProxyEvent
from ascii_colors import trace_exception

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/evaluations/{ds_id}/explore", response_class=HTMLResponse, name="admin_explore_dataset")
async def admin_explore_dataset(ds_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    ds = await db.get(BenchmarkDataset, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    context = get_template_context(request)
    context["dataset"] = ds
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/explore_dataset.html", context)

@router.post("/api/evaluations/datasets/{ds_id}/generate_card", name="api_eval_generate_ds_card")
async def api_generate_ds_card(ds_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    ds = await db.get(BenchmarkDataset, ds_id)
    if not ds: return JSONResponse({"error": "Dataset not found"}, status_code=404)
    
    agent = request.app.state.settings.admin_agent_name
    if not agent: return JSONResponse({"error": "No Management Agent set"}, status_code=500)

    # Sample items for the AI to understand the dataset
    items_sample = ds.content[:10]
    
    prompt = (
        f"Generate a professional Hugging Face Dataset Card (README.md) for the following dataset.\n"
        f"NAME: {ds.name}\nDESCRIPTION: {ds.description}\n"
        f"CONTENT SAMPLE: {json.dumps(items_sample)}\n\n"
        "Include sections for 'Dataset Summary', 'Supported Tasks', 'Languages', and 'Dataset Structure'. "
        "Use the standard HF YAML metadata header at the top. Output ONLY the markdown text."
    )
    
    try:
        real_model, msgs = await _resolve_target(db, agent, [{"role": "user", "content": prompt}], request=request)
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Compute node offline"}, status_code=503)

        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False}).encode(), is_subrequest=True)
        
        card_text = json.loads(resp.body.decode()).get("message", {}).get("content", "").strip()
        ds.dataset_card = card_text
        await db.commit()
        return {"success": True, "card": card_text}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/evaluations/hf/info", name="api_eval_hf_info")
async def api_hf_info(repo_id: str, admin_user: User = Depends(require_admin_user)):
    """Fetches structure (columns/splits) of a Hugging Face dataset."""
    import pipmaster as pm
    pm.ensure_packages(["datasets", "pandas"])
    from datasets import load_dataset_builder
    try:
        builder = load_dataset_builder(repo_id)
        # Get splits and features (columns)
        info = builder.info
        return {
            "success": True,
            "splits": list(info.splits.keys()) if info.splits else ["train"],
            "columns": list(info.features.keys()) if info.features else [],
            "description": info.description or ""
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@router.post("/api/evaluations/hf/auto_map", name="api_eval_hf_automap")
async def api_hf_automap(
    request: Request,
    repo_id: str = Form(...),
    columns: str = Form(...),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Uses AI to guess the column mapping for a varied HF dataset."""
    agent = request.app.state.settings.admin_agent_name
    if not agent: return JSONResponse({"error": "Management Agent not set"}, status_code=500)

    prompt = (
        f"I am importing a Hugging Face dataset: '{repo_id}'.\n"
        f"COLUMNS FOUND: {columns}\n\n"
        "Identify which column is likely the 'prompt' (question/instruction) and which is the 'reference_answer'. "
        "Optionally identify a 'category' column. Output ONLY a JSON object: "
        '{"prompt": "col_name", "answer": "col_name", "category": "col_name"}'
    )
    
    try:
        real_model, msgs = await _resolve_target(db, agent, [{"role": "user", "content": prompt}], request=request)
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Compute offline"}, status_code=503)

        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False}).encode(), is_subrequest=True)
        raw = json.loads(resp.body.decode()).get("message", {}).get("content", "").strip()
        clean_json = re.sub(r'```(?:json)?\s*([\s\S]*?)```', r'\1', raw).strip()
        return json.loads(clean_json)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/evaluations/hf/pull", name="api_eval_hf_pull")
async def api_hf_pull(
    request: Request,
    repo_id: str = Form(...),
    split: str = Form(...),
    col_prompt: str = Form(...),
    col_answer: str = Form(...),
    col_category: Optional[str] = Form(None),
    limit: int = Form(500),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Downloads and ingests items from HF into a new Dataset."""
    from datasets import load_dataset
    try:
        # Load specific split with streaming to handle large datasets
        ds = load_dataset(repo_id, split=split, streaming=True)
        
        items = []
        count = 0
        for row in ds:
            if count >= limit: break
            
            prompt_val = row.get(col_prompt)
            answer_val = row.get(col_answer)
            
            # Handle list/nested formats common in conversational datasets
            if isinstance(prompt_val, list): prompt_val = str(prompt_val[0])
            if isinstance(answer_val, list): answer_val = str(answer_val[0])

            items.append({
                "category": str(row.get(col_category, "hf_import")) if col_category else "hf_import",
                "prompt": str(prompt_val),
                "reference_answer": str(answer_val),
                "source_chunk": f"Imported from {repo_id} split {split}"
            })
            count += 1

        if not items: raise Exception("No items found in split.")

        new_ds = BenchmarkDataset(
            name=f"HF: {repo_id.split('/')[-1]}",
            description=f"Imported from HF Hub: {repo_id}",
            content=items
        )
        db.add(new_ds)
        await db.commit()
        return {"success": True, "count": len(items)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/evaluations/datasets/{ds_id}/push_hf", name="api_eval_push_to_hf")
async def api_push_to_hf(
    ds_id: int, 
    repo_id: str = Form(...),
    hf_token: str = Form(...),
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user)
):
    import pipmaster as pm
    pm.ensure_packages(["huggingface_hub"])
    from huggingface_hub import HfApi
    
    ds = await db.get(BenchmarkDataset, ds_id)
    if not ds: return JSONResponse({"error": "Dataset not found"}, status_code=404)

    try:
        api = HfApi()
        # 1. Create/Validate Repo
        api.create_repo(repo_id=repo_id, token=hf_token, repo_type="dataset", exist_ok=True)
        
        # 2. Prepare files in memory
        content_json = json.dumps(ds.content, indent=2).encode("utf-8")
        readme = (ds.dataset_card or f"# {ds.name}\n\n{ds.description}").encode("utf-8")
        
        # 3. Upload
        api.upload_file(path_or_fileobj=content_json, path_in_repo="dataset.json", repo_id=repo_id, token=hf_token)
        api.upload_file(path_or_fileobj=readme, path_in_repo="README.md", repo_id=repo_id, token=hf_token)
        
        return {"success": True, "url": f"https://huggingface.co/datasets/{repo_id}"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/evaluations", response_class=HTMLResponse, name="admin_evaluations")
async def admin_evaluations_page(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    
    # Load necessary data for selectors
    res_ds = await db.execute(select(DataStore).order_by(DataStore.name))
    context["datastores"] = res_ds.scalars().all()
    
    res_ds_list = await db.execute(select(BenchmarkDataset).order_by(desc(BenchmarkDataset.created_at)))
    context["datasets"] = res_ds_list.scalars().all()
    
    res_runs = await db.execute(select(BenchmarkRun).order_by(desc(BenchmarkRun.created_at)))
    context["runs"] = res_runs.scalars().all()
    
    # Fetch models for benchmarking
    context["all_models"] = await server_crud.get_all_available_model_names(db, filter_type='chat')
    context["embed_models"] = await server_crud.get_all_available_model_names(db, filter_type='embedding')
    
    # NEW: Fetch active workflows to allow benchmarking agentic capacity
    from app.database.models import Workflow
    res_wf = await db.execute(select(Workflow.name).filter(Workflow.is_active == True))
    context["workflows"] = sorted(res_wf.scalars().all())

    return templates.TemplateResponse("admin/evaluations.html", context)

async def _generate_dataset_task_multi(request: Request, ds_ids: list, texts: list, name: str, desc: str, chunks_per_source: int, admin_username: str, task_id: str):
    try:
        dataset_items =[]
        app_settings = request.app.state.settings
        agent = app_settings.admin_agent_name
        if not agent: raise Exception("Management Agent not set in Hub settings")
        
        all_chunks =[]
        
        async with AsyncSessionLocal() as db:
            # 1. Process Datastores
            if ds_ids:
                import pipmaster as pm
                pm.ensure_packages(["safe-store"])
                from safe_store import SafeStore
                from app.api.v1.routes.datastores import VECTORIZER_MAP
                
                for ds_id in ds_ids:
                    ds = await db.get(DataStore, ds_id)
                    if not ds: continue
                    
                    event_manager.emit(ProxyEvent("active", task_id, "Evaluator", "Local", admin_username, error_message=f"Extracting chunks from datastore: {ds.name}..."))
                    
                    def _get_ds_chunks(db_path, v_name, v_config):
                        s = SafeStore(db_path=db_path, vectorizer_name=v_name, vectorizer_config=v_config)
                        with s:
                            results = s.query("summary overview concept process data", top_k=min(200, chunks_per_source * 5))
                            return[r.get("chunk_text") for r in results if r.get("chunk_text")]
                            
                    v_key = VECTORIZER_MAP.get(ds.vectorizer_name, ds.vectorizer_name)
                    ds_chunks = await run_in_threadpool(_get_ds_chunks, ds.db_path, v_key, ds.vectorizer_config or {})
                    if ds_chunks:
                        sampled = random.sample(ds_chunks, min(chunks_per_source, len(ds_chunks)))
                        all_chunks.extend(sampled)
                        
            # 2. Process Raw Texts & Uploaded Files
            import textwrap
            for item in texts:
                title = item.get("title", "Document")
                content = item.get("content", "")
                if not content.strip(): continue
                
                event_manager.emit(ProxyEvent("active", task_id, "Evaluator", "Local", admin_username, error_message=f"Chunking source: {title}..."))
                # Break into roughly 2000 character chunks for localized generation
                chunks = textwrap.wrap(content, width=2000, break_long_words=False, replace_whitespace=False)
                sampled = random.sample(chunks, min(chunks_per_source, len(chunks)))
                all_chunks.extend(sampled)
                
            if not all_chunks:
                raise Exception("No knowledge chunks could be extracted from the provided sources.")
            
            # 3. Generate Tasks (CONCURRENT EXECUTION)
            total_chunks = len(all_chunks)
            update_lock = asyncio.Lock()

            async def process_single_chunk(idx, chunk_text):
                nonlocal dataset_items
                sub_id = f"{task_id}_gen_{idx}"
                
                event_manager.emit(ProxyEvent("active", task_id, "Evaluator", agent, admin_username, 
                                            error_message=f"Chunk {idx+1}/{total_chunks}: Prompting AI..."))

                prompt = (
                    "Given the following knowledge chunk, generate 2 diverse, complex tasks (e.g., Q&A, coding, analysis) "
                    "that can be COMPLETELY answered using ONLY this chunk. Output ONLY a JSON list of objects with keys "
                    "'category', 'prompt', and 'reference_answer'. Do not use markdown backticks.\n\n"
                    f"CHUNK:\n{chunk_text[:4000]}"
                )
                
                try:
                    # We need a fresh session per concurrent branch to avoid SQLite threading errors
                    async with AsyncSessionLocal() as sub_db:
                        real_model, msgs = await _resolve_target(sub_db, agent, [{"role": "user", "content": prompt}], request=request, request_id=sub_id, sender=admin_username)
                        servers = await server_crud.get_servers_with_model(sub_db, real_model)
                        if not servers: return

                        resp, chosen_srv = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False, "options": {"temperature": 0.2}}).encode(), 
                                                              is_subrequest=True, request_id=sub_id, model=real_model, sender=admin_username)
                        
                        if hasattr(resp, 'body'):
                            data = json.loads(resp.body.decode())
                            raw = data.get("message", {}).get("content", "").strip()
                            clean_json = re.sub(r'```(?:json)?\s*([\s\S]*?)```', r'\1', raw).strip()
                            
                            start = clean_json.find('[')
                            end = clean_json.rfind(']')
                            if start != -1 and end != -1:
                                items = json.loads(clean_json[start:end+1])
                                async with update_lock:
                                    for item in items:
                                        if isinstance(item, dict) and "prompt" in item and "reference_answer" in item:
                                            item["source_chunk"] = chunk_text
                                            dataset_items.append(item)
                                    
                                    # Update global progress bar
                                    current_progress = int((len(dataset_items) / (total_chunks * 2)) * 100)
                                    event_manager.emit(ProxyEvent("active", task_id, "Evaluator", "Local", admin_username, 
                                                                error_message=f"Progress: {len(dataset_items)} tasks generated.", 
                                                                token_count=current_progress))
                except Exception as e:
                    logger.error(f"Chunk {idx} failed: {e}")

            # Run up to 5 chunks in parallel to avoid overwhelming the cluster
            semaphore = asyncio.Semaphore(5)
            async def sem_task(idx, txt):
                async with semaphore:
                    await process_single_chunk(idx, txt)

            await asyncio.gather(*(sem_task(i, c) for i, c in enumerate(all_chunks)))
                        
            if not dataset_items:
                raise Exception("Failed to generate any valid dataset items from the chunks.")
                
            new_ds = BenchmarkDataset(name=name, description=desc, content=dataset_items)
            db.add(new_ds)
            await db.commit()
            
            event_manager.emit(ProxyEvent("completed", task_id, "Evaluator", "Local", admin_username, error_message=f"Dataset '{name}' generated with {len(dataset_items)} items.", token_count=100))
    except Exception as e:
        event_manager.emit(ProxyEvent("error", task_id, "Evaluator", "Local", admin_username, error_message=str(e)))
        trace_exception(e)

async def _run_benchmark_task(request: Request, run_name: str, dataset_id: int, models: List[str], evaluator_type: str, evaluator_config: dict, admin_username: str, task_id: str):
    try:
        async with AsyncSessionLocal() as db:
            dataset = await db.get(BenchmarkDataset, dataset_id)
            if not dataset: raise Exception("Dataset not found")
            
            items = dataset.content
            results = {model:[] for model in models}
            total_evals = len(models) * len(items)
            current_eval = 0
            
            vectorizer = None
            if evaluator_type == "vectorizer":
                def _load_vec():
                    import pipmaster as pm
                    pm.ensure_packages(["safe-store", "numpy"])
                    from safe_store.vectorization.manager import VectorizationManager
                    manager = VectorizationManager()
                    
                    v_name = evaluator_config.get("name", "ollama")
                    v_conf = evaluator_config.get("config", {}).copy()
                    
                    # Auto-inject Hub host for Ollama method to use internal compute nodes
                    if v_name == "ollama":
                        # Use bootstrap_settings because PROXY_PORT is an environment config, not a DB setting
                        v_conf["host"] = f"http://127.0.0.1:{bootstrap_settings.PROXY_PORT}"
                        
                        # INTERNAL AUTH FIX: Use the Hub's SECRET_KEY to bypass its own API Key check.
                        # The dependency in v1/dependencies.py explicitly allows this key.
                        v_conf["api_key"] = bootstrap_settings.SECRET_KEY

                    # Increase timeout for the loopback connection to handle slow model loading
                    if "timeout" not in v_conf:
                        v_conf["timeout"] = 60.0

                    return manager.get_vectorizer(v_name, v_conf)
                vectorizer = await run_in_threadpool(_load_vec)
                
            for model in models:
                for i, item in enumerate(items):
                    current_eval += 1
                    progress = int((current_eval / total_evals) * 100)
                    event_manager.emit(ProxyEvent("active", task_id, "Evaluator", model, admin_username, error_message=f"Evaluating {model} ({current_eval}/{total_evals})...", token_count=progress))
                    
                    # 1. Get Model Answer
                    sub_id = f"{task_id}_eval_{current_eval}"
                    real_model, msgs = await _resolve_target(db, model, [{"role": "user", "content": item["prompt"]}], request=request, request_id=sub_id, sender=admin_username)
                    
                    model_answer = ""
                    if real_model == "__result__":
                        # Workflow returned a static result (e.g. from an internal Composer or Agent loop)
                        model_answer = msgs[-1]["content"] if msgs else ""
                    else:
                        servers = await server_crud.get_servers_with_model(db, real_model)
                        if servers:
                            resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False, "options": {"temperature": 0.0}}).encode(), is_subrequest=True, request_id=sub_id, model=real_model, sender=admin_username)
                            if hasattr(resp, 'body'):
                                data = json.loads(resp.body.decode())
                                model_answer = data.get("message", {}).get("content", "").strip()
                            
                    # 2. Evaluate
                    score = 0.0
                    reasoning = ""
                    
                    if not model_answer:
                        score = 0.0
                        reasoning = "Model failed to return an answer or is offline."
                    elif evaluator_type == "vectorizer":
                        def _sim():
                            import numpy as np
                            from safe_store.search.similarity import cosine_similarity
                            embs = vectorizer.vectorize([item["reference_answer"], model_answer])
                            # Convert lists to numpy arrays to satisfy safe_store requirements
                            query_vec = np.array(embs[0])
                            target_vecs = np.array([embs[1]])
                            s = cosine_similarity(query_vec, target_vecs)[0]
                            return float((s + 1) / 2) # Normalize from [-1, 1] to [0, 1]
                        score = await run_in_threadpool(_sim)
                        score = float(score)
                        reasoning = f"Cosine similarity against reference answer using {evaluator_config.get('name', 'st')}."
                    elif evaluator_type == "llm":
                        judge_model = evaluator_config.get("judge_model", request.app.state.settings.admin_agent_name)
                        judge_prompt = f"Evaluate the model's answer against the reference answer.\nPROMPT: {item['prompt']}\nREFERENCE: {item['reference_answer']}\nMODEL ANSWER: {model_answer}\n\nScore the model's answer from 0.0 to 1.0 based on accuracy and completeness. Output ONLY a JSON object: {{\"score\": 0.85, \"reasoning\": \"...\"}}"
                        
                        judge_sub_id = f"{task_id}_judge_{current_eval}"
                        j_real_model, j_msgs = await _resolve_target(db, judge_model,[{"role": "user", "content": judge_prompt}], request=request, request_id=judge_sub_id, sender=admin_username)
                        j_servers = await server_crud.get_servers_with_model(db, j_real_model)
                        if j_servers:
                            j_resp, _ = await _reverse_proxy(request, "chat", j_servers, json.dumps({"model": j_real_model, "messages": j_msgs, "stream": False, "options": {"temperature": 0.0}}).encode(), is_subrequest=True, request_id=judge_sub_id, model=j_real_model, sender=admin_username)
                            if hasattr(j_resp, 'body'):
                                j_data = json.loads(j_resp.body.decode())
                                j_raw = j_data.get("message", {}).get("content", "").strip()
                                j_clean = re.sub(r'```(?:json)?\s*([\s\S]*?)```', r'\1', j_raw).strip()
                                try:
                                    j_start = j_clean.find('{')
                                    j_end = j_clean.rfind('}')
                                    j_obj = json.loads(j_clean[j_start:j_end+1])
                                    score = float(j_obj.get("score", 0.0))
                                    reasoning = j_obj.get("reasoning", "")
                                except:
                                    score = 0.0
                                    reasoning = f"Judge failed to return valid JSON: {j_raw}"
                    
                    results[model].append({
                        "prompt": item["prompt"],
                        "reference_answer": item["reference_answer"],
                        "model_answer": model_answer,
                        "score": score,
                        "reasoning": reasoning
                    })
                    
            # Save Run
            new_run = BenchmarkRun(
                dataset_id=dataset_id,
                name=run_name,
                models=models,
                evaluator_config={"type": evaluator_type, "config": evaluator_config},
                results=results
            )
            db.add(new_run)
            await db.commit()
            event_manager.emit(ProxyEvent("completed", task_id, "Evaluator", "Local", admin_username, error_message=f"Benchmark '{run_name}' completed.", token_count=100))
    except Exception as e:
        event_manager.emit(ProxyEvent("error", task_id, "Evaluator", "Local", admin_username, error_message=str(e)))
        trace_exception(e)

from fastapi import File, UploadFile
from app.core import knowledge_importer as kit

@router.post("/api/evaluations/datasets/generate", name="api_eval_generate_dataset")
async def api_generate_dataset(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    description: str = Form(""),
    chunks_per_source: int = Form(5),
    datastores: str = Form("[]"),
    raw_texts: str = Form("[]"),
    files: Optional[List[UploadFile]] = File(None),
    admin_user: User = Depends(require_admin_user)
):
    try:
        ds_ids = json.loads(datastores)
        texts = json.loads(raw_texts)
        
        # We must process UploadFile objects synchronously before passing to the background task,
        # as FastAPI closes file descriptors when the HTTP response is returned.
        valid_files = [f for f in files if f and f.filename] if files else[]
        if valid_files:
            # Reusing the existing extractor which parses PDFs, Docx, etc.
            content = await kit.extract_local_file_content(valid_files)
            texts.append({"title": "Uploaded Files Bundle", "content": content})

        task_id = f"sys_eval_ds_{secrets.token_hex(4)}"
        background_tasks.add_task(
            _generate_dataset_task_multi, 
            request, ds_ids, texts, name, description, chunks_per_source, admin_user.username, task_id
        )
        return {"success": True, "task_id": task_id, "message": "Dataset generation started in background."}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@router.post("/api/evaluations/run", name="api_eval_run_benchmark")
async def api_run_benchmark(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    dataset_id: int = Form(...),
    models: str = Form(...),
    evaluator_type: str = Form(...), # "vectorizer" or "llm"
    vectorizer_model: str = Form(None),
    judge_model: str = Form(None),
    admin_user: User = Depends(require_admin_user)
):
    model_list =[m.strip() for m in models.split(",") if m.strip()]
    if not model_list: return JSONResponse({"error": "No models selected"}, status_code=400)
    
    config = {}
    if evaluator_type == "vectorizer":
        v_method = Form(None) # Not standard, manually extracted from request below
    
    # Surgical extraction of method since it was added to the template but not yet to route args
    form_data = await request.form()
    v_method = form_data.get("vectorizer_method", "ollama")

    if evaluator_type == "vectorizer":
        config = {"name": v_method, "config": {"model": vectorizer_model}}
    else:
        config = {"judge_model": judge_model}
        
    task_id = f"sys_eval_run_{secrets.token_hex(4)}"
    background_tasks.add_task(_run_benchmark_task, request, name, dataset_id, model_list, evaluator_type, config, admin_user.username, task_id)
    return {"success": True, "task_id": task_id, "message": "Benchmark started in background."}

@router.delete("/api/evaluations/datasets/{ds_id}", name="api_eval_delete_dataset")
async def api_delete_dataset(ds_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    ds = await db.get(BenchmarkDataset, ds_id)
    if ds:
        await db.delete(ds)
        await db.commit()
    return {"success": True}

@router.delete("/api/evaluations/runs/{run_id}", name="api_eval_delete_run")
async def api_delete_run(run_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    run = await db.get(BenchmarkRun, run_id)
    if run:
        await db.delete(run)
        await db.commit()
    return {"success": True}

@router.get("/evaluations/runs/{run_id}/report", response_class=HTMLResponse, name="admin_evaluation_report")
async def admin_evaluation_report(run_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    run = await db.get(BenchmarkRun, run_id)
    if not run: raise HTTPException(status_code=404)
    
    # 1. Advanced Statistics Aggregation
    stats = {}
    category_scores = {} # {model: {cat: [scores]}}
    
    for model, results in run.results.items():
        scores = [r["score"] for r in results]
        stats[model] = {
            "avg": sum(scores) / len(scores) if scores else 0,
            "min": min(scores) if scores else 0,
            "max": max(scores) if scores else 0,
            "std": float(np.std(scores)) if scores else 0,
            "count": len(scores)
        }
        
        category_scores[model] = {}
        for r in results:
            cat = r.get("category", "General")
            if cat not in category_scores[model]: category_scores[model][cat] = []
            category_scores[model][cat].append(r["score"])

    # Calculate average per category
    cat_summary = {} # {cat: {model: avg}}
    for model, cats in category_scores.items():
        for cat, scores in cats.items():
            if cat not in cat_summary: cat_summary[cat] = {}
            cat_summary[cat][model] = sum(scores) / len(scores)

    context = get_template_context(request)
    context.update({
        "run": run,
        "stats": stats,
        "cat_summary": cat_summary,
        "csrf_token": await get_csrf_token(request)
    })
    return templates.TemplateResponse("admin/evaluation_report.html", context)

@router.get("/evaluations/runs/{run_id}/export-pdf", name="admin_export_run_pdf")
async def admin_export_run_pdf(run_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from xhtml2pdf import pisa
    import io
    import os
    from fastapi.concurrency import run_in_threadpool

    # Reuse the report logic to get the HTML
    response = await admin_evaluation_report(run_id, request, db, admin_user)
    html_content = response.body.decode()

    def link_callback(uri, rel):
        """
        Convert HTML URIs to local filesystem paths so xhtml2pdf can access them
        without making internal HTTP requests.
        """
        # handle /static/ and /uploads/
        if uri.startswith('/static/'):
            path = os.path.join("app", uri.lstrip('/'))
        elif uri.startswith('/static/uploads/'):
            path = os.path.join("app", uri.lstrip('/'))
        else:
            return uri

        if not os.path.isfile(path):
            logger.warning(f"PDF Export: Asset not found at {path}")
            return uri
        return path

    def generate_pdf(content):
        pdf_buffer = io.BytesIO()
        # Create PDF using local file access
        pisa_status = pisa.CreatePDF(content, dest=pdf_buffer, link_callback=link_callback)
        if pisa_status.err:
            return None
        return pdf_buffer.getvalue()

    # Execute CPU-bound PDF generation in a separate thread
    pdf_data = await run_in_threadpool(generate_pdf, html_content)
    
    if pdf_data is None:
        raise HTTPException(status_code=500, detail="PDF Generation Failed (Internal Engine Error)")
        
    return Response(
        pdf_data, 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename=Benchmark_Report_{run_id}.pdf"}
    )

@router.post("/api/evaluations/runs/{run_id}/analyze", name="api_eval_analyze_run")
async def api_eval_analyze_run(run_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    """Uses the Hub Agent to provide qualitative analysis of the benchmark results."""
    run = await db.get(BenchmarkRun, run_id)
    if not run: return JSONResponse({"error": "Run not found"}, status_code=404)
    
    agent = request.app.state.settings.admin_agent_name
    if not agent: return JSONResponse({"error": "Management Agent not configured"}, status_code=400)

    # Prepare a condensed data view for the AI
    summary_data = {
        "name": run.name,
        "evaluator": run.evaluator_config,
        "models": run.models,
        "results_summary": {}
    }
    
    for m, res in run.results.items():
        avg = sum(r["score"] for r in res) / len(res)
        weakest = sorted(res, key=lambda x: x["score"])[0]
        summary_data["results_summary"][m] = {
            "average_score": avg,
            "worst_case_prompt": weakest["prompt"],
            "worst_case_score": weakest["score"]
        }

    prompt = (
        f"You are the Lead Evaluator for LoLLMs Hub. Analyze the following benchmark data.\n"
        f"DATA: {json.dumps(summary_data)}\n\n"
        "1. Identify the 'Winner' and why.\n"
        "2. Identify specific logical weaknesses or biases in the losing models.\n"
        "3. Provide 3 actionable deployment recommendations.\n"
        "Format with clear, professional Markdown."
    )
    
    try:
        real_model, msgs = await _resolve_target(db, agent, [{"role": "user", "content": prompt}], request=request)
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Compute offline"}, status_code=503)

        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": msgs, "stream": False}).encode(), is_subrequest=True)
        analysis = json.loads(resp.body.decode()).get("message", {}).get("content", "Analysis failed.")
        return {"analysis": analysis}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/evaluations/runs/{run_id}/data", name="api_eval_run_data")
async def api_get_run_data(run_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    run = await db.get(BenchmarkRun, run_id)
    if not run: raise HTTPException(status_code=404)
    return {"name": run.name, "models": run.models, "config": run.evaluator_config, "results": run.results}

@router.get("/api/evaluations/datasets/{ds_id}/items", name="api_eval_get_dataset_items")
async def api_get_dataset_items(ds_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    ds = await db.get(BenchmarkDataset, ds_id)
    if not ds: raise HTTPException(status_code=404)
    return {"name": ds.name, "description": ds.description, "items": ds.content}

@router.post("/api/evaluations/datasets/{ds_id}/items", name="api_eval_add_dataset_item")
async def api_add_dataset_item(
    ds_id: int, 
    request: Request,
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user)
):
    data = await request.json()
    ds = await db.get(BenchmarkDataset, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    # Validation
    if not data.get("prompt") or not data.get("reference_answer"):
        raise HTTPException(status_code=400, detail="Prompt and Answer are required")

    # Append to JSON list
    items = list(ds.content)
    items.append({
        "category": data.get("category", "manual"),
        "prompt": data["prompt"],
        "reference_answer": data["reference_answer"],
        "source_chunk": "Expert Manual Entry"
    })
    
    ds.content = items
    await db.commit()
    return {"success": True}

@router.put("/api/evaluations/datasets/{ds_id}/items/{index}", name="api_eval_update_dataset_item")
async def api_update_dataset_item(
    ds_id: int, index: int,
    request: Request,
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user)
):
    data = await request.json()
    ds = await db.get(BenchmarkDataset, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    items = list(ds.content)
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="Item index out of bounds")

    items[index].update({
        "category": data.get("category", items[index]["category"]),
        "prompt": data.get("prompt", items[index]["prompt"]),
        "reference_answer": data.get("reference_answer", items[index]["reference_answer"])
    })
    
    ds.content = items
    await db.commit()
    return {"success": True}

@router.delete("/api/evaluations/datasets/{ds_id}/items/{index}", name="api_eval_delete_dataset_item")
async def api_delete_dataset_item(
    ds_id: int, index: int,
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user)
):
    ds = await db.get(BenchmarkDataset, ds_id)
    if not ds: raise HTTPException(status_code=404)
    
    items = list(ds.content)
    if 0 <= index < len(items):
        items.pop(index)
        ds.content = items
        await db.commit()
        return {"success": True}
    return JSONResponse({"error": "Invalid index"}, status_code=400)