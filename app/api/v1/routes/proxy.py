import asyncio
import json
import logging
import datetime
from typing import List, Tuple, Optional, Dict, Any
from fastapi import APIRouter, Depends, Request, Response, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.api.v1.dependencies import get_valid_api_key, rate_limiter, ip_filter, get_settings
from app.database.models import APIKey, OllamaServer
from app.crud import log_crud, server_crud, model_metadata_crud
from app.core.retry import retry_with_backoff, RetryConfig
from app.schema.settings import AppSettingsModel
from app.core.encryption import decrypt_data
from app.core.vllm_translator import (
    translate_ollama_to_vllm_chat,
    translate_ollama_to_vllm_embeddings,
    translate_vllm_to_ollama_embeddings,
    vllm_stream_to_ollama_stream
)

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
    """
    normalized_url = server.url.rstrip('/')
    backend_url = f"{normalized_url}/api/{path}"

    request_headers = headers.copy()
    if server.encrypted_api_key:
        api_key = decrypt_data(server.encrypted_api_key)
        if api_key:
            request_headers["Authorization"] = f"Bearer {api_key}"

    backend_request = http_client.build_request(
        method=method,
        url=backend_url,
        headers=request_headers,
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

        # --- BRANCH: Handle vLLM servers differently ---
        if chosen_server.server_type == 'vllm':
            try:
                # vLLM translation doesn't use the retry logic wrapper in the same way
                response = await _proxy_to_vllm(request, chosen_server, path, body_bytes)
                return response, chosen_server
            except HTTPException:
                raise # Re-raise HTTP exceptions from the vLLM proxy
            except Exception as e:
                logger.warning(f"vLLM server '{chosen_server.name}' failed: {e}. Trying next server.")
                continue # Try next server

        # --- Ollama server logic (with retries) ---
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


async def _proxy_to_vllm(
    request: Request,
    server: OllamaServer,
    path: str,
    body_bytes: bytes
) -> Response:
    """
    Handles proxying a request to a vLLM server, including payload and response translation.
    """
    http_client: AsyncClient = request.app.state.http_client
    
    try:
        ollama_payload = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model_name = ollama_payload.get("model")
    
    headers = {}
    if server.encrypted_api_key:
        api_key = decrypt_data(server.encrypted_api_key)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    # Translate path and payload based on the endpoint
    if path == "chat":
        vllm_path = "v1/chat/completions"
        vllm_payload = translate_ollama_to_vllm_chat(ollama_payload)
    elif path == "embeddings":
        vllm_path = "v1/embeddings"
        vllm_payload = translate_ollama_to_vllm_embeddings(ollama_payload)
    else:
        raise HTTPException(status_code=404, detail=f"Endpoint '/api/{path}' not supported for vLLM servers.")
        
    backend_url = f"{server.url.rstrip('/')}/{vllm_path}"
    is_streaming = vllm_payload.get("stream", False)

    try:
        if is_streaming:
            async def stream_generator():
                async with http_client.stream("POST", backend_url, json=vllm_payload, timeout=600.0, headers=headers) as vllm_response:
                    if vllm_response.status_code != 200:
                        error_body = await vllm_response.aread()
                        logger.error(f"vLLM server error ({vllm_response.status_code}): {error_body.decode()}")
                        # Yield a single error chunk in Ollama format
                        error_chunk = {"error": f"vLLM server error: {error_body.decode()}"}
                        yield (json.dumps(error_chunk) + '\n').encode('utf-8')
                        return
                    
                    async for chunk in vllm_stream_to_ollama_stream(vllm_response.aiter_text(), model_name):
                        yield chunk
            
            return StreamingResponse(stream_generator(), media_type="application/x-ndjson")
        else: # Non-streaming
            response = await http_client.post(backend_url, json=vllm_payload, timeout=600.0, headers=headers)
            response.raise_for_status()
            vllm_data = response.json()
            
            if path == "embeddings":
                ollama_data = translate_vllm_to_ollama_embeddings(vllm_data)
                return JSONResponse(content=ollama_data)
            # Add non-streaming chat translation if needed
            raise NotImplementedError("Non-streaming chat for vLLM not yet implemented.")

    except httpx.HTTPStatusError as e:
        error_detail = e.response.text
        logger.error(f"vLLM request failed with status {e.response.status_code}: {error_detail}")
        raise HTTPException(status_code=e.response.status_code, detail=f"vLLM server error: {error_detail}")
    except Exception as e:
        logger.error(f"Error proxying to vLLM server {server.name}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to communicate with vLLM server: {e}")


@router.get("/tags")
async def federate_models(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db)
):
    """
    Aggregates models from all configured backends (Ollama and vLLM)
    using the cached model data from the database for efficiency.
    """
    logger.info("--- /tags endpoint: Starting model federation ---")
    all_servers = await server_crud.get_servers(db)
    servers = [s for s in all_servers if s.is_active]
    logger.info(f"/tags: Found {len(servers)} active servers.")

    all_models = {}
    for server in servers:
        logger.info(f"/tags: Processing server '{server.name}' (type: {server.server_type})")
        models_list = server.available_models or []
        
        raw_models_repr = repr(models_list)
        if len(raw_models_repr) > 300:
            raw_models_repr = raw_models_repr[:300] + '... (truncated)'
        logger.info(f"/tags: Raw 'available_models' for '{server.name}': {raw_models_repr}")

        if isinstance(models_list, str):
            try:
                models_list = json.loads(models_list)
                logger.info(f"/tags: Successfully parsed JSON string for '{server.name}'")
            except json.JSONDecodeError:
                logger.warning(f"/tags: Could not parse available_models JSON for server {server.name}")
                continue

        if not isinstance(models_list, list):
            logger.warning(f"/tags: Field available_models for server {server.name} is not a list. Type is {type(models_list)}")
            continue

        model_count_on_server = 0
        for model in models_list:
            if isinstance(model, dict) and "name" in model:
                # --- FIX: Ensure 'model' key exists for compatibility with Ollama clients ---
                if "model" not in model:
                    model["model"] = model["name"]
                # --- END FIX ---
                all_models[model['name']] = model
                model_count_on_server += 1
            else:
                logger.warning(f"/tags: Invalid model format found for server '{server.name}': {model}")
        
        logger.info(f"/tags: Added {model_count_on_server} models from server '{server.name}'")

    logger.info(f"/tags: Total unique models before adding 'auto': {len(all_models)}")

    # Add the 'auto' model to the list for clients to see, with details for compatibility
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

    await log_crud.create_usage_log(
        db=db, api_key_id=api_key.id, endpoint="/api/tags", status_code=200, server_id=None
    )
    
    final_model_list = list(all_models.values())
    logger.info("--- /tags endpoint: Finished model federation ---")

    return {"models": final_model_list}


async def _select_auto_model(db: AsyncSession, body: Dict[str, Any]) -> Optional[str]:
    """Selects the best model based on metadata and request content."""
    
    # 1. Determine request characteristics
    has_images = "images" in body and body["images"]
    
    prompt_content = ""
    if "prompt" in body: # generate endpoint
        prompt_content = body["prompt"]
    elif "messages" in body: # chat endpoint
        last_message = body["messages"][-1] if body["messages"] else {}
        if isinstance(last_message.get("content"), str):
            prompt_content = last_message["content"]
        elif isinstance(last_message.get("content"), list): # multimodal chat
             text_part = next((p.get("text", "") for p in last_message["content"] if p.get("type") == "text"), "")
             prompt_content = text_part

    code_keywords = ["def ", "class ", "import ", "const ", "let ", "var ", "function ", "public static void", "int main("]
    contains_code = any(kw.lower() in prompt_content.lower() for kw in code_keywords)

    # 2. Get all model metadata and available models
    all_metadata = await model_metadata_crud.get_all_metadata(db)
    all_available_models = await server_crud.get_all_available_model_names(db)
    available_metadata = [m for m in all_metadata if m.model_name in all_available_models]

    if not available_metadata:
        logger.warning("Auto-routing failed: No models with metadata are available on active servers.")
        return None

    # 3. Filter models based on characteristics
    candidate_models = available_metadata

    if has_images:
        logger.info("Auto-routing: Filtering for models that support images.")
        candidate_models = [m for m in candidate_models if m.supports_images]
    
    if contains_code:
        logger.info("Auto-routing: Filtering for code models.")
        code_models = [m for m in candidate_models if m.is_code_model]
        if code_models:
            candidate_models = code_models

    if body.get("options", {}).get("fast_model"):
        logger.info("Auto-routing: Filtering for fast models.")
        fast_models = [m for m in candidate_models if m.is_fast_model]
        if fast_models:
            candidate_models = fast_models

    if not candidate_models:
        logger.warning("Auto-routing: No models matched the request criteria. Falling back to the highest priority model available.")
        candidate_models = available_metadata

    if not candidate_models:
        return None

    # 4. The list is already sorted by priority from the CRUD function.
    best_model = candidate_models[0]
    logger.info(f"Auto-routing selected model '{best_model.model_name}' with priority {best_model.priority}.")
    
    return best_model.model_name


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_ollama(
    request: Request,
    path: str,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db),
    settings: AppSettingsModel = Depends(get_settings),
    servers: List[OllamaServer] = Depends(get_active_servers),
):
    """
    A catch-all route that proxies all other requests to the backend.
    Uses smart routing and translates requests for vLLM servers.
    """
    # --- Endpoint Security Check ---
    blocked_paths = {p.strip().lstrip('/') for p in settings.blocked_ollama_endpoints.split(',') if p.strip()}
    request_path = path.strip().lstrip('/')

    if request_path in blocked_paths:
        logger.warning(
            f"Blocked attempt to access sensitive endpoint '/api/{request_path}' by API key {api_key.key_prefix}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access to the endpoint '/api/{request_path}' is disabled by the proxy administrator."
        )

    # Try to extract model name from request body
    body_bytes = await request.body()
    model_name = None
    body = {}

    if body_bytes:
        try:
            body = json.loads(body_bytes)
            if isinstance(body, dict) and "model" in body:
                model_name = body["model"]
        except (json.JSONDecodeError, Exception):
            pass

    # Handle 'think' parameter based on model support
    if model_name and isinstance(body, dict) and "think" in body:
        model_name_lower = model_name.lower()
        supported_think_models = ["qwen", "gpt-oss", "deepseek"]
        
        is_supported = any(keyword in model_name_lower for keyword in supported_think_models)

        if is_supported:
            # Handle special case for gpt-oss which requires string values if boolean `true` is passed
            if "gpt-oss" in model_name_lower and body.get("think") is True:
                logger.info(f"Translating 'think: true' to 'think: \"medium\"' for GPT-OSS model '{model_name}'")
                body["think"] = "medium"
                body_bytes = json.dumps(body).encode('utf-8')
        else:
            # If the model is not supported, remove the 'think' parameter to avoid errors.
            logger.warning(f"Model '{model_name}' is not in the known list for 'think' support. Removing 'think' parameter from request to avoid errors.")
            del body["think"]
            body_bytes = json.dumps(body).encode('utf-8')
            
    # --- NEW: Handle 'auto' model routing ---
    if model_name == "auto":
        chosen_model_name = await _select_auto_model(db, body)
        if not chosen_model_name:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auto-routing could not find an available and suitable model."
            )
        
        # Override the model in the request and continue
        model_name = chosen_model_name
        body["model"] = model_name
        body_bytes = json.dumps(body).encode('utf-8')

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