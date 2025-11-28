# app/api/v1/routes/playground_embedding.py
import logging
import json
import asyncio
from typing import List, Dict, Any, Optional
from pathlib import Path

import numpy as np
from sklearn.decomposition import PCA
from pydantic import BaseModel, conlist, AnyHttpUrl
import httpx

from fastapi import APIRouter, Depends, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.database.models import User
from app.crud import server_crud
from app.api.v1.dependencies import validate_csrf_token_header
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.core.benchmarks import PREBUILT_BENCHMARKS


logger = logging.getLogger(__name__)
router = APIRouter()

# --- Pydantic Models for Benchmark ---
class BenchmarkGroup(BaseModel):
    id: str
    name: str
    color: str
    texts: List[str]

class BenchmarkPayload(BaseModel):
    name: str
    groups: List[BenchmarkGroup]

class BenchmarkRequest(BaseModel):
    models: conlist(str, min_length=1)
    benchmark: BenchmarkPayload

# --- Helper Function ---
async def get_embedding(
    http_client: httpx.AsyncClient, 
    server: "OllamaServer", 
    model_name: str, 
    prompt: str
) -> List[float]:
    """Helper to get a single embedding from a server."""
    from app.crud.server_crud import _get_auth_headers
    headers = _get_auth_headers(server)
    try:
        if server.server_type == 'vllm':
            from app.core.vllm_translator import translate_ollama_to_vllm_embeddings, translate_vllm_to_ollama_embeddings
            url = f"{server.url.rstrip('/')}/v1/embeddings"
            payload = translate_ollama_to_vllm_embeddings({"model": model_name, "prompt": prompt})
            response = await http_client.post(url, json=payload, timeout=60.0, headers=headers)
            response.raise_for_status()
            return translate_vllm_to_ollama_embeddings(response.json())["embedding"]
        else: # Ollama
            url = f"{server.url.rstrip('/')}/api/embeddings"
            payload = {"model": model_name, "prompt": prompt}
            response = await http_client.post(url, json=payload, timeout=60.0, headers=headers)
            response.raise_for_status()
            return response.json()["embedding"]

    except httpx.HTTPStatusError as e:
        logger.error(f"Error getting embedding for model {model_name}: {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=f"Backend server error: {e.response.text}")
    except Exception as e:
        logger.error(f"Failed to connect to backend server at {server.url}: {e}")
        raise HTTPException(status_code=500, detail=f"Could not connect to backend server to get embedding: {e}")

# --- Routes ---
@router.get("/embedding-playground", response_class=HTMLResponse, name="admin_embedding_playground")
async def admin_embedding_playground_ui(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    from app.api.v1.dependencies import get_csrf_token
    context = get_template_context(request)
    context["models"] = await server_crud.get_all_available_model_names(db, filter_type='embedding')
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/embedding_playground.html", context)

@router.get("/embedding-playground/prebuilt", name="admin_get_prebuilt_benchmarks")
async def admin_get_prebuilt_benchmarks(admin_user: User = Depends(require_admin_user)):
    all_benchmarks = list(PREBUILT_BENCHMARKS)
    benchmarks_dir = Path("benchmarks")
    if benchmarks_dir.is_dir():
        for filepath in benchmarks_dir.glob("*.json"):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "name" in data and "groups" in data and isinstance(data["groups"], list):
                        all_benchmarks.append(data)
                    else:
                        logger.warning(f"Skipping invalid benchmark file: {filepath.name}")
            except Exception as e:
                logger.error(f"Failed to load benchmark file {filepath.name}: {e}")
    return JSONResponse(all_benchmarks)

@router.post("/embedding-playground/benchmark", name="admin_run_embedding_benchmark")
async def admin_run_embedding_benchmark(
    request_data: BenchmarkRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    csrf_valid: bool = Depends(validate_csrf_token_header)
):
    http_client: httpx.AsyncClient = request.app.state.http_client

    all_texts = {text for group in request_data.benchmark.groups for text in group.texts}
    
    model_embeddings = {}
    
    for model_name in request_data.models:
        servers = await server_crud.get_servers_with_model(db, model_name)
        if not servers:
            model_embeddings[model_name] = {"error": "Model not found on any active server."}
            continue
        
        server = servers[0]
        
        tasks = {text: get_embedding(http_client, server, model_name, text) for text in all_texts}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        
        embeddings_map = {text: result for text, result in zip(tasks.keys(), results)}
        model_embeddings[model_name] = embeddings_map

    response_data = {"models": {}, "groups": request_data.benchmark.model_dump()["groups"]}
    
    for model_name, embeddings_map in model_embeddings.items():
        if "error" in embeddings_map:
            response_data["models"][model_name] = {"error": embeddings_map["error"]}
            continue
            
        points = []
        vectors = []
        labels = []
        
        for group in request_data.benchmark.groups:
            for text in group.texts:
                vector = embeddings_map.get(text)
                if isinstance(vector, list):
                    points.append({"label": text, "group_id": group.id})
                    vectors.append(vector)
                    labels.append(group.name)

        if len(vectors) < 2:
            response_data["models"][model_name] = {"error": "Not enough valid data points for analysis."}
            continue

        pca = PCA(n_components=2)
        reduced_vectors = pca.fit_transform(np.array(vectors))
        
        for i, point in enumerate(points):
            point['x'] = float(reduced_vectors[i, 0])
            point['y'] = float(reduced_vectors[i, 1])
            
        response_data["models"][model_name] = {"points": points}
        
    return JSONResponse(response_data)
