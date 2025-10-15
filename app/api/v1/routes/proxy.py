import asyncio
import json
import logging
from typing import List, Tuple, Optional
from fastapi import APIRouter, Depends, Request, Response, HTTPException, status
from fastapi.responses import StreamingResponse
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.api.v1.dependencies import get_valid_api_key, rate_limiter, ip_filter
from app.database.models import APIKey, OllamaServer
from app.crud import log_crud, server_crud

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(ip_filter), Depends(rate_limiter)])

# --- Dependency to get active servers ---
async def get_active_servers(db: AsyncSession = Depends(get_db)) -> List[OllamaServer]:
    servers = await server_crud.get_servers(db)
    active_servers = [s for s in servers if s.is_active]
    if not active_servers:
        logger.error("No active Ollama backend servers are configured in the database.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No active backend servers available."
        )
    return active_servers

async def extract_model_from_request(request: Request) -> Optional[str]:
    """
    Attempts to extract the model name from the request body.
    Common endpoints that contain model info: /api/generate, /api/chat, /api/embeddings, /api/pull, etc.

    Returns the model name if found, otherwise None.
    """
    try:
        # Read the request body
        body_bytes = await request.body()

        if not body_bytes:
            return None

        # Parse JSON body
        body = json.loads(body_bytes)

        # Most Ollama API endpoints use "model" field
        if isinstance(body, dict) and "model" in body:
            return body["model"]

    except (json.JSONDecodeError, UnicodeDecodeError, Exception) as e:
        logger.debug(f"Could not extract model from request body: {e}")

    return None


async def _reverse_proxy(request: Request, path: str, servers: List[OllamaServer], body_bytes: bytes = b"") -> Tuple[Response, OllamaServer]:
    """
    Core reverse proxy logic. Forwards the request to a backend Ollama server
    and streams the response back. Returns the response and the chosen server.

    Args:
        request: The original FastAPI request
        path: The path to proxy to
        servers: List of servers to choose from
        body_bytes: Pre-read request body (if already read for model extraction)
    """
    http_client: AsyncClient = request.app.state.http_client

    if not hasattr(request.app.state, 'backend_server_index'):
        request.app.state.backend_server_index = 0

    index = request.app.state.backend_server_index
    chosen_server = servers[index]
    request.app.state.backend_server_index = (index + 1) % len(servers)

    normalized_url = chosen_server.url.rstrip('/')
    backend_url = f"{normalized_url}/api/{path}"

    headers = {k: v for k, v in request.headers.items() if k.lower() != 'host'}

    # Use pre-read body if available, otherwise stream from request
    if body_bytes:
        backend_request = http_client.build_request(
            method=request.method,
            url=backend_url,
            headers=headers,
            params=request.query_params,
            content=body_bytes
        )
    else:
        backend_request = http_client.build_request(
            method=request.method,
            url=backend_url,
            headers=headers,
            params=request.query_params,
            content=request.stream()
        )

    try:
        backend_response = await http_client.send(backend_request, stream=True)
    except Exception as e:
        logger.error(f"Error connecting to backend server {chosen_server.url}: {e}")
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Could not connect to backend server.")

    response = StreamingResponse(
        backend_response.aiter_raw(),
        status_code=backend_response.status_code,
        headers=backend_response.headers,
    )
    return response, chosen_server

@router.get("/tags")
async def federate_models(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db),
    servers: List[OllamaServer] = Depends(get_active_servers),
):
    """
    Aggregates models from all configured Ollama backends.
    """
    http_client: AsyncClient = request.app.state.http_client
    
    async def fetch_models(server_url: str):
        try:
            normalized_url = server_url.rstrip('/')
            response = await http_client.get(f"{normalized_url}/api/tags")
            response.raise_for_status()
            return response.json().get("models", [])
        except Exception as e:
            logger.error(f"Failed to fetch models from {server_url}: {e}")
            return []

    tasks = [fetch_models(server.url) for server in servers]
    results = await asyncio.gather(*tasks)

    all_models = {}
    for model_list in results:
        for model in model_list:
            all_models[model['name']] = model

    await log_crud.create_usage_log(
        db=db, api_key_id=api_key.id, endpoint="/api/tags", status_code=200, server_id=None
    )
    
    return {"models": list(all_models.values())}


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_ollama(
    request: Request,
    path: str,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db),
    servers: List[OllamaServer] = Depends(get_active_servers),
):
    """
    A catch-all route that proxies all other requests to the Ollama backend.
    Uses smart routing to select servers that have the requested model.
    """
    # Try to extract model name from request body
    body_bytes = await request.body()
    model_name = None

    if body_bytes:
        try:
            body = json.loads(body_bytes)
            if isinstance(body, dict) and "model" in body:
                model_name = body["model"]
        except (json.JSONDecodeError, Exception):
            pass

    # Smart routing: filter servers by model availability
    candidate_servers = servers
    if model_name:
        servers_with_model = await server_crud.get_servers_with_model(db, model_name)

        if servers_with_model:
            candidate_servers = servers_with_model
            logger.info(f"Smart routing: Found {len(servers_with_model)} server(s) with model '{model_name}'")
        else:
            # Model not found in any server's catalog, or catalogs not fetched yet
            # Fall back to all active servers
            logger.warning(
                f"Model '{model_name}' not found in any server's catalog. "
                f"Falling back to round-robin across all {len(servers)} active server(s). "
                f"Make sure to refresh model lists for accurate routing."
            )

    # Proxy to one of the candidate servers
    response, chosen_server = await _reverse_proxy(request, path, candidate_servers, body_bytes)

    await log_crud.create_usage_log(
        db=db,
        api_key_id=api_key.id,
        endpoint=f"/api/{path}",
        status_code=response.status_code,
        server_id=chosen_server.id,
        model=model_name
    )

    return response