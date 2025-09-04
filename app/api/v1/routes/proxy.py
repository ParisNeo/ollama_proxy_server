import asyncio
import logging
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database.session import get_db
from app.api.v1.dependencies import get_valid_api_key, rate_limiter, ip_filter
from app.database.models import APIKey
from app.crud import log_crud

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(ip_filter), Depends(rate_limiter)])


async def _reverse_proxy(request: Request, path: str):
    """
    Core reverse proxy logic. Forwards the request to a backend Ollama server
    and streams the response back.
    """
    http_client: AsyncClient = request.app.state.http_client
    
    # Simple load balancing: round-robin
    if not hasattr(request.app.state, 'backend_server_index'):
        request.app.state.backend_server_index = 0
    
    index = request.app.state.backend_server_index
    backend_url_base = settings.OLLAMA_SERVERS[index]
    request.app.state.backend_server_index = (index + 1) % len(settings.OLLAMA_SERVERS)

    backend_url = f"{backend_url_base}/{path}"

    url = http_client.build_request(
        method=request.method,
        url=backend_url,
        headers=request.headers,
        params=request.query_params,
        content=request.stream(),
    ).url

    headers = {k: v for k, v in request.headers.items() if k.lower() != 'host'}

    backend_request = http_client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=request.stream()
    )

    backend_response = await http_client.send(backend_request, stream=True)

    return StreamingResponse(
        backend_response.aiter_raw(),
        status_code=backend_response.status_code,
        headers=backend_response.headers,
    )

@router.get("/tags")
async def federate_models(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Aggregates models from all configured Ollama backends.
    """
    http_client: AsyncClient = request.app.state.http_client
    
    async def fetch_models(url):
        try:
            response = await http_client.get(f"{url}/api/tags")
            response.raise_for_status()
            return response.json().get("models", [])
        except Exception as e:
            logger.error(f"Failed to fetch models from {url}: {e}")
            return []

    tasks = [fetch_models(server) for server in settings.OLLAMA_SERVERS]
    results = await asyncio.gather(*tasks)

    all_models = {}
    for model_list in results:
        for model in model_list:
            all_models[model['name']] = model

    await log_crud.create_usage_log(
        db=db, api_key_id=api_key.id, endpoint="/api/tags", status_code=200
    )
    
    return {"models": list(all_models.values())}


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_ollama(
    request: Request,
    path: str,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    A catch-all route that proxies all other requests to the Ollama backend.
    """
    response = await _reverse_proxy(request, path)
    
    await log_crud.create_usage_log(
        db=db, api_key_id=api_key.id, endpoint=f"/{path}", status_code=response.status_code
    )
    
    return response