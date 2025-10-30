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
from app.core.retry import retry_with_backoff, RetryConfig
from app.schema.settings import AppSettingsModel

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


async def _send_backend_request(
    http_client: AsyncClient,
    server: OllamaServer,
    path: str,
    method: str,
    headers: dict,
    query_params,
    body_bytes: bytes
):
    """
    Internal function to send a single request to a backend server.
    This function is wrapped by retry logic.

    Args:
        http_client: The HTTP client to use
        server: The server to send the request to
        path: The API path
        method: HTTP method
        headers: Request headers
        query_params: Query parameters
        body_bytes: Request body

    Returns:
        The HTTP response from the backend server

    Raises:
        Exception on any connection or HTTP error
    """
    normalized_url = server.url.rstrip('/')
    backend_url = f"{normalized_url}/api/{path}"

    backend_request = http_client.build_request(
        method=method,
        url=backend_url,
        headers=headers,
        params=query_params,
        content=body_bytes
    )

    try:
        backend_response = await http_client.send(backend_request, stream=True)

        # Consider 5xx errors as failures that should be retried
        if backend_response.status_code >= 500:
            await backend_response.aclose()  # Clean up the response
            raise Exception(
                f"Backend server returned {backend_response.status_code}: "
                f"{backend_response.reason_phrase}"
            )

        return backend_response

    except Exception as e:
        # Log and re-raise for retry logic
        logger.debug(f"Request to {server.url} failed: {type(e).__name__}: {str(e)}")
        raise


async def _reverse_proxy(request: Request, path: str, servers: List[OllamaServer], body_bytes: bytes = b"") -> Tuple[Response, OllamaServer]:
    """
    Core reverse proxy logic with retry support. Forwards the request to a backend
    Ollama server and streams the response back. Returns the response and the chosen server.

    Features:
    - Round-robin load balancing across available servers
    - Automatic retries with exponential backoff on failures
    - Falls back to other servers if one is unavailable
    - Configurable retry behavior via app settings

    Args:
        request: The original FastAPI request
        path: The path to proxy to
        servers: List of servers to choose from
        body_bytes: Pre-read request body (if already read for model extraction)

    Returns:
        Tuple of (Response, OllamaServer) - the streaming response and the server that handled it

    Raises:
        HTTPException: If all retry attempts fail across all available servers
    """
    http_client: AsyncClient = request.app.state.http_client
    app_settings: AppSettingsModel = request.app.state.settings

    # Get retry configuration from app settings
    retry_config = RetryConfig(
        max_retries=app_settings.max_retries,
        total_timeout_seconds=app_settings.retry_total_timeout_seconds,
        base_delay_ms=app_settings.retry_base_delay_ms
    )

    if not hasattr(request.app.state, 'backend_server_index'):
        request.app.state.backend_server_index = 0

    # Prepare request headers (exclude 'host' header)
    headers = {k: v for k, v in request.headers.items() if k.lower() != 'host'}

    # Try each server in round-robin fashion
    # If we have N servers, we'll try each one with retries before giving up
    num_servers = len(servers)
    servers_tried = []

    for server_attempt in range(num_servers):
        # Select next server using round-robin
        index = request.app.state.backend_server_index
        chosen_server = servers[index]
        request.app.state.backend_server_index = (index + 1) % len(servers)

        servers_tried.append(chosen_server.name)

        logger.info(
            f"Attempting request to server '{chosen_server.name}' "
            f"({server_attempt + 1}/{num_servers})"
        )

        # Attempt request with retries to this specific server
        retry_result = await retry_with_backoff(
            _send_backend_request,
            http_client=http_client,
            server=chosen_server,
            path=path,
            method=request.method,
            headers=headers,
            query_params=request.query_params,
            body_bytes=body_bytes,
            config=retry_config,
            retry_on_exceptions=(Exception,),
            operation_name=f"Request to {chosen_server.name}"
        )

        if retry_result.success:
            # Success! Create streaming response
            backend_response = retry_result.result

            logger.info(
                f"Successfully proxied to '{chosen_server.name}' "
                f"after {retry_result.attempts} attempt(s) "
                f"in {retry_result.total_duration_ms:.1f}ms"
            )

            response = StreamingResponse(
                backend_response.aiter_raw(),
                status_code=backend_response.status_code,
                headers=backend_response.headers,
            )
            return response, chosen_server
        else:
            # This server failed after all retries, try next server
            logger.warning(
                f"Server '{chosen_server.name}' failed after {retry_result.attempts} "
                f"attempts. Trying next server if available."
            )

    # All servers exhausted
    logger.error(
        f"All {num_servers} backend server(s) failed after retries. "
        f"Servers tried: {', '.join(servers_tried)}"
    )
    raise HTTPException(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        detail=f"All backend servers unavailable. Tried: {', '.join(servers_tried)}"
    )

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