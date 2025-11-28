import asyncio
import json
import logging
import datetime
import time
from typing import List, Tuple, Optional, Dict, Any
from fastapi import APIRouter, Depends, Request, Response, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
import httpx
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
from app.core.openrouter_translator import (
    translate_ollama_to_openrouter_chat,
    translate_ollama_to_openrouter_embeddings,
    translate_openrouter_to_ollama_embeddings,
    openrouter_stream_to_ollama_stream,
    get_openrouter_headers,
    OPENROUTER_BASE_URL
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
    # Ollama uses /api/{path}, not /api/v1/{path}
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

    # Prepare request headers (exclude 'host' and 'content-length' headers)
    # Let httpx calculate content-length automatically based on body_bytes
    headers = {k: v for k, v in request.headers.items() 
               if k.lower() not in ('host', 'content-length')}

    # Try each server in round-robin fashion
    num_servers = len(servers)
    if num_servers == 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No servers available for routing"
        )
    
    servers_tried = []

    for server_attempt in range(num_servers):
        # Select next server using round-robin
        if not hasattr(request.app.state, 'backend_server_index'):
            request.app.state.backend_server_index = 0
        index = request.app.state.backend_server_index % num_servers
        chosen_server = servers[index]
        request.app.state.backend_server_index = (index + 1) % num_servers

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

        # --- BRANCH: Handle OpenRouter servers differently ---
        if chosen_server.server_type == 'openrouter':
            try:
                response = await _proxy_to_openrouter(request, chosen_server, path, body_bytes)
                return response, chosen_server
            except HTTPException as e:
                # Check if this is an auto model and it failed with a model-specific error
                body_dict = json.loads(body_bytes) if body_bytes else {}
                is_auto = body_dict.get("_is_auto_model", False)
                
                if is_auto:
                    error_detail = str(e.detail).lower()
                    is_model_error = (
                        e.status_code in (404, 400) and 
                        ("model" in error_detail or "not found" in error_detail or "no such" in error_detail or "endpoint" in error_detail)
                    )
                    
                    if is_model_error:
                        # Model failed, try next best model
                        failed_model = body_dict.get("model")
                        logger.warning(f"Auto-routing: Model '{failed_model}' failed ({e.status_code}): {e.detail}. Trying next best model...")
                        
                        # Get next best model (skip the failed one)
                        settings = await get_settings()
                        skip_models = body_dict.get("_failed_models", [])
                        skip_models.append(failed_model)
                        body_dict["_failed_models"] = skip_models
                        
                        next_model = await _select_auto_model(db, body_dict, settings, skip_models=skip_models)
                        if next_model:
                            logger.info(f"Auto-routing fallback: Selected '{next_model}'")
                            body_dict["model"] = next_model
                            body_bytes = json.dumps(body_dict).encode('utf-8')
                            # Retry with next model
                            try:
                                response = await _proxy_to_openrouter(request, chosen_server, path, body_bytes)
                                return response, chosen_server
                            except HTTPException:
                                # Still failed, re-raise original error
                                raise e
                
                raise # Re-raise HTTP exceptions from the OpenRouter proxy
            except Exception as e:
                logger.warning(f"OpenRouter server '{chosen_server.name}' failed: {e}. Trying next server.")
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


async def _proxy_to_openrouter(
    request: Request,
    server: OllamaServer,
    path: str,
    body_bytes: bytes
) -> Response:
    """
    Handles proxying a request to OpenRouter, including payload and response translation.
    Supports all OpenRouter features: auto-routing, model routing, provider routing, etc.
    """
    http_client: AsyncClient = request.app.state.http_client
    
    try:
        ollama_payload = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model_name = ollama_payload.get("model")  # Can be None for auto-routing
    
    # Get API key from server
    server_api_key = None
    if server.encrypted_api_key:
        server_api_key = decrypt_data(server.encrypted_api_key)
    
    if not server_api_key:
        raise HTTPException(
            status_code=400, 
            detail="OpenRouter requires an API key. Please configure one in server settings."
        )
    
    # Build headers with OpenRouter-specific options
    # TODO: Could add HTTP-Referer and X-Title from server config or app settings
    headers = get_openrouter_headers(server_api_key)

    # Translate path and payload based on the endpoint
    if path == "chat":
        openrouter_path = "chat/completions"
        openrouter_payload = translate_ollama_to_openrouter_chat(ollama_payload)
    elif path == "embeddings":
        openrouter_path = "embeddings"
        openrouter_payload = translate_ollama_to_openrouter_embeddings(ollama_payload)
    else:
        raise HTTPException(
            status_code=404, 
            detail=f"Endpoint '/api/{path}' not supported for OpenRouter servers. Supported: /api/chat, /api/embeddings"
        )
    
    # OpenRouter always uses the same base URL
    backend_url = f"{OPENROUTER_BASE_URL}/{openrouter_path}"
    is_streaming = openrouter_payload.get("stream", False)

    try:
        if is_streaming:
            async def stream_generator():
                async with http_client.stream(
                    "POST", 
                    backend_url, 
                    json=openrouter_payload, 
                    timeout=600.0, 
                    headers=headers
                ) as openrouter_response:
                    if openrouter_response.status_code != 200:
                        error_body = await openrouter_response.aread()
                        logger.error(f"OpenRouter server error ({openrouter_response.status_code}): {error_body.decode()}")
                        # Yield a single error chunk in Ollama format
                        error_chunk = {"error": f"OpenRouter server error: {error_body.decode()}"}
                        yield (json.dumps(error_chunk) + '\n').encode('utf-8')
                        return
                    
                    async for chunk in openrouter_stream_to_ollama_stream(
                        openrouter_response.aiter_text(), 
                        model_name
                    ):
                        yield chunk
            
            return StreamingResponse(stream_generator(), media_type="application/x-ndjson")
        else:  # Non-streaming
            response = await http_client.post(
                backend_url, 
                json=openrouter_payload, 
                timeout=600.0, 
                headers=headers
            )
            response.raise_for_status()
            openrouter_data = response.json()
            
            if path == "embeddings":
                ollama_data = translate_openrouter_to_ollama_embeddings(openrouter_data)
                return JSONResponse(content=ollama_data)
            elif path == "chat":
                # Translate non-streaming chat response
                choices = openrouter_data.get("choices", [])
                if choices:
                    message = choices[0].get("message", {})
                    actual_model = openrouter_data.get("model", model_name)
                    ollama_data = {
                        "model": actual_model or "openrouter",
                        "created_at": datetime.datetime.fromtimestamp(
                            openrouter_data.get("created", time.time()),
                            tz=datetime.timezone.utc
                        ).isoformat().replace('+00:00', 'Z'),
                        "message": message,
                        "done": True,
                    }
                    return JSONResponse(content=ollama_data)
                else:
                    raise HTTPException(status_code=500, detail="OpenRouter returned empty response")
            else:
                raise HTTPException(status_code=404, detail=f"Unsupported endpoint: {path}")

    except httpx.HTTPStatusError as e:
        try:
            error_detail = e.response.json()
        except:
            error_detail = e.response.text
        logger.error(f"OpenRouter request failed with status {e.response.status_code}: {error_detail}")
        raise HTTPException(
            status_code=e.response.status_code, 
            detail=f"OpenRouter server error: {error_detail}"
        )
    except Exception as e:
        logger.error(f"Error proxying to OpenRouter server {server.name}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to communicate with OpenRouter server: {e}"
        )


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


async def _get_all_models_ollama_format(db: AsyncSession) -> Dict[str, Dict[str, Any]]:
    """
    Helper function to get all models in Ollama format.
    Returns a dict mapping model_name -> model_dict.
    """
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
    
    # Add the 'auto' model
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


async def _select_auto_model(db: AsyncSession, body: Dict[str, Any], settings: AppSettingsModel = None, skip_models: List[str] = None) -> Optional[str]:
    """
    Professional auto-routing system that intelligently selects models based on:
    - Request characteristics (images, code, tool calling, internet, thinking, fast)
    - Model capabilities (from metadata)
    - Model descriptions (semantic matching)
    - Priority modes (Free, Daily Drive, Advanced, Luxury)
    - Budget considerations
    """
    from app.core.auto_router import AutoRouter
    
    # Get priority mode from settings (default to "free")
    priority_mode = "free"
    if settings:
        priority_mode = getattr(settings, "auto_routing_priority_mode", "free") or "free"
    
    # Initialize the professional auto-router
    router = AutoRouter(priority_mode=priority_mode)
    
    # Analyze the request to determine what capabilities are needed
    request_analysis = router.analyze_request(body)
    
    # Get all model metadata and available models
    all_metadata = await model_metadata_crud.get_all_metadata(db)
    all_available_models = await server_crud.get_all_available_model_names(db)
    available_metadata = [m for m in all_metadata if m.model_name in all_available_models]

    if not available_metadata:
        logger.warning("Auto-routing failed: No models with metadata are available on active servers.")
        return None

    # Build model_details_map from server.available_models (for pricing information)
    model_details_map = {}
    servers = await server_crud.get_servers(db)
    active_servers = [s for s in servers if s.is_active]
    
    for server in active_servers:
        if server.available_models:
            models_list = server.available_models
            if isinstance(models_list, str):
                try:
                    models_list = json.loads(models_list)
                except:
                    continue
            
            for model_data in models_list:
                if isinstance(model_data, dict) and "name" in model_data:
                    model_name = model_data["name"]
                    if model_name not in model_details_map:
                        model_details_map[model_name] = model_data.get("details", {})
    
    # Use priority-based routing: try each priority level in order
    priorities = sorted(set(m.priority for m in available_metadata if m.priority is not None))
    
    # Filter out skipped models (for fallback retry)
    if skip_models:
        available_metadata = [m for m in available_metadata if m.model_name not in skip_models]
        if not available_metadata:
            logger.warning(f"Auto-routing: All models skipped. Tried: {skip_models}")
            return None
    
    if not priorities:
        # No priorities set - use all available models
        logger.warning("Auto-routing: No models have priority set. Using all available models.")
        result = router.select_best_model(available_metadata, request_analysis, model_details_map)
        if result:
            model, score = result
            return model.model_name
        return None
    
    # Try each priority level in order (1 = highest priority)
    for priority_level in priorities:
        priority_models = [m for m in available_metadata if m.priority == priority_level]
        
        if not priority_models:
            continue
        
        logger.info(f"Auto-routing ({priority_mode}): Checking {len(priority_models)} models with priority {priority_level}")
        
        # Score and select best model from this priority level
        result = router.select_best_model(priority_models, request_analysis, model_details_map)
        
        if result:
            model, score = result
            # Only return if score is positive (model has required capabilities)
            if score > 0:
                return model.model_name
            else:
                logger.debug(f"Model '{model.model_name}' scored {score:.2f} (missing required capabilities), trying next priority level")
        
        # No suitable model at this priority level, continue to next priority
    
    # If we get here, no models matched at any priority level
    # Fall back to highest priority model available (regardless of characteristics)
    logger.warning("Auto-routing: No models matched the request criteria at any priority level. Falling back to highest priority model.")
    if priorities:
        fallback_priority = priorities[0]
        fallback_models = [m for m in available_metadata if m.priority == fallback_priority]
        if fallback_models:
            result = router.select_best_model(fallback_models, request_analysis, model_details_map)
            if result:
                model, score = result
                logger.info(f"Auto-routing fallback: Selected '{model.model_name}' with priority {model.priority} (score: {score:.2f}).")
                return model.model_name
    
    # Last resort: return first available model
    if available_metadata:
        logger.warning("Auto-routing: Using first available model as last resort.")
        return available_metadata[0].model_name
    
    return None


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
        # Get settings for auto-routing priority mode
        settings = await get_settings()
        chosen_model_name = await _select_auto_model(db, body, settings)
        if not chosen_model_name:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auto-routing could not find an available and suitable model."
            )
        
        # Override the model in the request and continue
        model_name = chosen_model_name
        body["model"] = model_name
        body["_is_auto_model"] = True  # Flag for fallback retry
        body_bytes = json.dumps(body).encode('utf-8')
    
    # --- WEB SEARCH INTEGRATION: Automatically enhance chat requests with web search when needed ---
    if path == "chat" and isinstance(body, dict) and "messages" in body:
        app_settings: AppSettingsModel = request.app.state.settings
        
        # Check if proxy web search is enabled
        if not app_settings.enable_proxy_web_search:
            logger.debug("Proxy web search is disabled in settings, skipping web search integration")
        else:
            has_api_key = bool(app_settings.ollama_api_key or app_settings.ollama_api_key_2)
            
            if has_api_key:
                from app.core.unified_search import UnifiedSearchService
                from app.core.chat_web_search import needs_web_search, extract_search_query, format_search_results_naturally
                
                # Get the last user message
                messages = body.get("messages", [])
                last_user_message = None
                for msg in reversed(messages):
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        last_user_message = msg
                        break
                
                if last_user_message:
                    user_content = last_user_message.get("content", "")
                    if isinstance(user_content, str) and needs_web_search(user_content):
                        logger.info(f"Web search triggered for proxy chat request: {user_content[:100]}")
                        try:
                            search_service = UnifiedSearchService(
                                searxng_url=app_settings.searxng_url if app_settings.searxng_url else "http://localhost:7019",
                                ollama_api_key=app_settings.ollama_api_key,
                                ollama_api_key_2=app_settings.ollama_api_key_2,
                                timeout=20.0
                            )
                            
                            search_query = extract_search_query(user_content)
                            logger.info(f"Performing web search for query: {search_query}")
                            
                            search_results = await search_service.web_search(
                                query=search_query,
                                max_results=5,
                                engine=None  # Auto: try SearXNG first, fallback to Ollama
                            )
                            
                            if search_results.get("results"):
                                logger.info(f"Web search returned {len(search_results['results'])} results")
                                # Format results as natural, flowing prose
                                web_context = format_search_results_naturally(search_results["results"], search_query)
                                
                                if web_context:
                                    # Add web search context to the user's message directly
                                    # This ensures it works with all server types (OpenRouter, vLLM, Ollama)
                                    original_content = user_content
                                    enhanced_content = f"Current information from the web:\n\n{web_context}\n\nBased on this information, please answer: {original_content}"
                                    last_user_message["content"] = enhanced_content
                                    
                                    # Update the body and re-encode body_bytes
                                    body["messages"] = messages
                                    body_bytes = json.dumps(body).encode('utf-8')
                                    
                                    logger.info(f"✓ Enhanced proxy chat message with web search context (original: {len(original_content)}, enhanced: {len(enhanced_content)})")
                                else:
                                    logger.warning("Web search returned results but formatting produced empty context")
                            else:
                                logger.warning(f"Web search returned no results for query: {search_query}")
                        except Exception as e:
                            logger.error(f"Error adding web search context to proxy request: {e}", exc_info=True)
                            # Continue without web search if it fails
                    else:
                        logger.debug(f"Web search not needed for proxy chat message: {user_content[:100] if isinstance(user_content, str) else 'non-string content'}")
            else:
                logger.debug("Ollama API keys not configured, skipping web search for proxy requests")
    # --- END WEB SEARCH INTEGRATION ---

    # Handle /api/show endpoint - Ollama-specific, return 404 for OpenRouter models
    # This endpoint is used by clients to get model details, but OpenRouter doesn't support it
    if path == "show":
        # Try to get model name from body if not already extracted
        if not model_name and body_bytes:
            try:
                body_data = json.loads(body_bytes)
                model_name = body_data.get("model")
            except:
                pass
        
        # If we have a model name, check if it's an OpenRouter model
        if model_name:
            # OpenRouter models have "provider/model" format
            if "/" in model_name and not model_name.startswith("/"):
                # Definitely an OpenRouter model
                logger.debug(f"/api/show requested for OpenRouter model '{model_name}' - returning 404")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Endpoint '/api/show' is not supported for OpenRouter models. Use OpenRouter's /models endpoint instead."
                )
            # Check servers to see if this model is only on OpenRouter servers
            servers_with_model = await server_crud.get_servers_with_model(db, model_name)
            if servers_with_model:
                # Check if all servers are OpenRouter (which doesn't support /api/show)
                if all(s.server_type == "openrouter" for s in servers_with_model):
                    logger.debug(f"/api/show requested for model '{model_name}' which is only on OpenRouter servers - returning 404")
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Endpoint '/api/show' is not supported for OpenRouter models. Use OpenRouter's /models endpoint instead."
                    )
        # For Ollama servers or unknown models, continue with normal proxying
    
    # Smart routing: filter servers by model availability
    candidate_servers = servers
    if model_name:
        servers_with_model = await server_crud.get_servers_with_model(db, model_name)

        if servers_with_model:
            candidate_servers = servers_with_model
            logger.info(f"Smart routing: Found {len(servers_with_model)} server(s) with model '{model_name}'")
        else:
            # Model not found in any server's catalog, or catalogs not fetched yet
            # Use intelligent fallback based on model name patterns
            model_name_lower = model_name.lower()
            
            # Ollama cloud models (with :cloud suffix) should only go to Ollama servers
            if ":cloud" in model_name_lower:
                ollama_servers = [s for s in servers if s.server_type == "ollama"]
                if ollama_servers:
                    candidate_servers = ollama_servers
                    logger.info(
                        f"Model '{model_name}' not found in catalog, but detected as Ollama cloud model. "
                        f"Routing to {len(ollama_servers)} Ollama server(s) only."
                    )
                else:
                    logger.error(f"Ollama cloud model '{model_name}' requested but no Ollama servers are active.")
            # OpenRouter models typically have "provider/model" format
            elif "/" in model_name and not model_name.startswith("/"):
                openrouter_servers = [s for s in servers if s.server_type == "openrouter"]
                if openrouter_servers:
                    candidate_servers = openrouter_servers
                    logger.info(
                        f"Model '{model_name}' not found in catalog, but detected as OpenRouter model. "
                        f"Routing to {len(openrouter_servers)} OpenRouter server(s) only."
                    )
            else:
                # Fall back to all active servers for unknown patterns
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