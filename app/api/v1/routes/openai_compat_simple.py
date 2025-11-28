"""
Simplified OpenAI-compatible endpoints - minimal implementation that doesn't break existing proxy
"""
import logging
import datetime
import json
import time
from typing import Dict, Any
from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
import httpx
from httpx import AsyncClient

from app.database.session import get_db
from app.api.v1.dependencies import get_valid_api_key, rate_limiter, ip_filter
from app.database.models import APIKey
from app.crud import log_crud, server_crud
from app.core.encryption import decrypt_data
from app.core.openrouter_translator import OPENROUTER_BASE_URL, get_openrouter_headers

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(ip_filter), Depends(rate_limiter)])


async def _get_all_models_ollama_format(db: AsyncSession) -> Dict[str, Dict[str, Any]]:
    """Get all models in Ollama format"""
    all_servers = await server_crud.get_servers(db)
    servers = [s for s in all_servers if s.is_active]
    
    all_models = {}
    for server in servers:
        models_list = server.available_models or []
        
        if isinstance(models_list, str):
            try:
                models_list = json.loads(models_list)
            except json.JSONDecodeError:
                continue

        if not isinstance(models_list, list):
            continue

        for model in models_list:
            if isinstance(model, dict) and "name" in model:
                if "model" not in model:
                    model["model"] = model["name"]
                all_models[model['name']] = model
    
    all_models["auto"] = {
        "name": "auto",
        "model": "auto",
        "modified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "size": 0,
        "digest": "auto-digest-placeholder",
        "details": {
            "parent_model": "",
            "format": "proxy",
            "family": "auto",
            "families": ["auto"],
            "parameter_size": "N/A",
            "quantization_level": "N/A"
        }
    }
    
    return all_models


@router.get("/v1/models")
@router.get("/models")
async def openai_models_endpoint(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db)
):
    """OpenAI-compatible models endpoint"""
    all_models = await _get_all_models_ollama_format(db)
    
    openai_models = []
    for model_name, model_data in all_models.items():
        model_id = model_data.get("name") or model_data.get("model", model_name)
        openai_models.append({
            "id": model_id,
            "object": "model",
            "created": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
            "owned_by": "ollama-proxy",
        })
    
    await log_crud.create_usage_log(
        db=db, api_key_id=api_key.id, endpoint="/v1/models", status_code=200, server_id=None
    )
    
    return {"object": "list", "data": openai_models}

