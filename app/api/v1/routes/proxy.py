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

# --- Connection Pool Cache ---
_server_health_cache: Dict[int, Dict[str, Any]] = {}
_health_cache_ttl_seconds = 5


def _is_server_healthy_cached(server_id: int) -> bool:
    """Check if server is healthy based on cached status."""
    import time
    cache_entry = _server_health_cache.get(server_id)
    if cache_entry:
        if time.time() - cache_entry["timestamp"] < _health_cache_ttl_seconds:
            return cache_entry["healthy"]
    return True


def _update_health_cache(server_id: int, healthy: bool):
    """Update the health cache for a server."""
    import time
    _server_health_cache[server_id] = {
        "timestamp": time.time(),
        "healthy": healthy
    }


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
    """
    try:
        body_bytes = await request.body()
        if not body_bytes:
            return None
        body = json.loads(body_bytes)
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
    """
    normalized_url = server.url.rstrip('/')
    backend_url = f"{normalized_url}/api/{path}"

    request_headers = {}
    
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in ('host', 'connection', 'keep-alive', 'proxy-authenticate', 
                       'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade'):
            continue
        if k_lower == 'content-length':
            continue
        request_headers[k] = v
    
    if body_bytes:
        request_headers['content-length'] = str(len(body_bytes))
    
    if server.encrypted_api_key:
        api_key = decrypt_data(server.encrypted_api_key)
        if api_key:
            request_headers["authorization"] = f"Bearer {api_key}"

    backend_request = http_client.build_request(
        method=method,
        url=backend_url,
        headers=request_headers,
        params=query_params,
        content=body_bytes
    )

    try:
        backend_response = await http_client.send(backend_request, stream=True)

        if backend_response.status_code >= 500:
            await backend_response.aclose()
            raise Exception(
                f"Backend server returned {backend_response.status_code}: "
                f"{backend_response.reason_phrase}"
            )

        return backend_response

    except Exception as e:
        logger.debug(f"Request to {server.url} failed: {type(e).__name__}: {str(e)[:200]}")
        raise


def _extract_tokens_from_chunk(chunk_data: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Extract token counts from an Ollama response chunk."""
    tokens = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    
    # Ollama format - check various field names
    if "prompt_eval_count" in chunk_data:
        tokens["prompt_tokens"] = chunk_data.get("prompt_eval_count")
    if "prompt_count" in chunk_data:
        tokens["prompt_tokens"] = chunk_data.get("prompt_count")
    if "eval_count" in chunk_data:
        tokens["completion_tokens"] = chunk_data.get("eval_count")
    
    # Calculate total if we have both
    if tokens["prompt_tokens"] is not None and tokens["completion_tokens"] is not None:
        tokens["total_tokens"] = tokens["prompt_tokens"] + tokens["completion_tokens"]
    
    # vLLM/OpenAI format (translated)
    if "usage" in chunk_data and chunk_data["usage"]:
        usage = chunk_data["usage"]
        if isinstance(usage, dict):
            tokens["prompt_tokens"] = usage.get("prompt_tokens")
            tokens["completion_tokens"] = usage.get("completion_tokens")
            tokens["total_tokens"] = usage.get("total_tokens")
    
    # Final chunk with done=True often has the complete stats
    if chunk_data.get("done"):
        if "prompt_eval_count" in chunk_data:
            tokens["prompt_tokens"] = chunk_data.get("prompt_eval_count")
        if "prompt_count" in chunk_data:
            tokens["prompt_tokens"] = chunk_data.get("prompt_count")
        if "eval_count" in chunk_data:
            tokens["completion_tokens"] = chunk_data.get("eval_count")
        
        if tokens["prompt_tokens"] is not None and tokens["completion_tokens"] is not None:
            tokens["total_tokens"] = tokens["prompt_tokens"] + tokens["completion_tokens"]
    
    return tokens


async def _update_log_with_tokens_async(
    log_id: int,
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    total_tokens: Optional[int]
):
    """Fire-and-forget token update."""
    try:
        from app.database.session import AsyncSessionLocal
        async with AsyncSessionLocal() as async_db:
            await log_crud.update_usage_log_with_tokens(
                async_db, log_id, prompt_tokens, completion_tokens, total_tokens
            )
    except Exception as e:
        logger.debug(f"Failed to update tokens for log {log_id}: {e}")


async def _reverse_proxy(request: Request, path: str, servers: List[OllamaServer], body_bytes: bytes = "", 
                        api_key_id: Optional[int] = None, log_id: Optional[int] = None) -> Tuple[Response, OllamaServer]:
    """
    Core reverse proxy logic with retry support and token tracking.
    """
    http_client: AsyncClient = request.app.state.http_client
    app_settings: AppSettingsModel = request.app.state.settings

    retry_config = RetryConfig(
        max_retries=app_settings.max_retries,
        total_timeout_seconds=app_settings.retry_total_timeout_seconds,
        base_delay_ms=app_settings.retry_base_delay_ms
    )

    headers = {k: v for k, v in request.headers.items() if k.lower() not in 
               ('host', 'connection', 'keep-alive', 'proxy-authenticate',
                'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade', 'content-length')}

    logger.info(f"_reverse_proxy called with {len(servers)} total server(s), filtering to active...")
    
    candidate_servers = [
        s for s in servers 
        if s.is_active and _is_server_healthy_cached(s.id)
    ]
    
    logger.info(f"After filtering: {len(candidate_servers)} active server(s): {[s.name for s in candidate_servers]}")
    
    if not candidate_servers:
        candidate_servers = [s for s in servers if s.is_active]
        if not candidate_servers:
            logger.error("All candidate servers became inactive during request processing")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No active backend servers available."
            )

    if not hasattr(request.app.state, 'backend_server_index'):
        request.app.state.backend_server_index = 0
        logger.info("Initialized backend_server_index to 0")
    
    current_index = request.app.state.backend_server_index % max(1, len(candidate_servers))
    request.app.state.backend_server_index = (current_index + 1) % max(1, len(candidate_servers))
    
    logger.info(f"Round-robin: current_index={current_index}, next_index will be {request.app.state.backend_server_index}, candidate_count={len(candidate_servers)}")

    servers_tried = []

    for server_attempt in range(len(candidate_servers)):
        safe_index = (current_index + server_attempt) % len(candidate_servers)
        chosen_server = candidate_servers[safe_index]
        
        logger.info(f"Server attempt {server_attempt + 1}/{len(candidate_servers)}: selected '{chosen_server.name}' at index {safe_index}")

        servers_tried.append(chosen_server.name)

        if chosen_server.server_type == 'vllm':
            logger.info(f"Using vLLM branch for server '{chosen_server.name}'")
            try:
                response = await _proxy_to_vllm(request, chosen_server, path, body_bytes, api_key_id, log_id)
                _update_health_cache(chosen_server.id, True)
                return response, chosen_server
            except HTTPException:
                _update_health_cache(chosen_server.id, False)
                raise
            except Exception as e:
                logger.warning(f"vLLM server '{chosen_server.name}' failed: {e}. Trying next server.")
                _update_health_cache(chosen_server.id, False)
                candidate_servers = [s for s in candidate_servers if s.id != chosen_server.id]
                if not candidate_servers:
                    logger.error("No more candidate servers after vLLM failure")
                    break
                current_index = safe_index % max(1, len(candidate_servers))
                continue

        logger.info(f"Using Ollama branch with retry logic for server '{chosen_server.name}'")
        
        first_attempt_start = asyncio.get_event_loop().time()
        
        try:
            backend_response = await _send_backend_request(
                http_client=http_client,
                server=chosen_server,
                path=path,
                method=request.method,
                headers=headers,
                query_params=request.query_params,
                body_bytes=body_bytes
            )
            
            first_attempt_duration = asyncio.get_event_loop().time() - first_attempt_start
            
            _update_health_cache(chosen_server.id, True)
            
            # Check if this is a streaming response
            is_streaming = _is_streaming_response(backend_response)
            
            if is_streaming and log_id:
                # Wrap for token tracking
                wrapped_response = _wrap_response_for_token_tracking(
                    backend_response, chosen_server, api_key_id, log_id, path
                )
                return wrapped_response, chosen_server
            else:
                # Non-streaming, return as-is (tokens will be extracted if possible)
                if log_id and backend_response.status_code == 200:
                    # Try to extract tokens from non-streaming response
                    try:
                        body = await backend_response.aread()
                        if body:
                            data = json.loads(body.decode('utf-8'))
                            tokens = _extract_tokens_from_chunk(data)
                            if tokens.get("total_tokens") is not None or tokens.get("prompt_tokens") is not None:
                                asyncio.create_task(_update_log_with_tokens_async(
                                    log_id,
                                    tokens["prompt_tokens"],
                                    tokens["completion_tokens"],
                                    tokens["total_tokens"]
                                ))
                        # Need to create a new response since we consumed the body
                        return Response(
                            content=body,
                            status_code=backend_response.status_code,
                            headers=dict(backend_response.headers)
                        ), chosen_server
                    except Exception:
                        pass
                # Return original response if we couldn't extract tokens
                return backend_response, chosen_server
            
        except Exception as first_error:
            _update_health_cache(chosen_server.id, False)
            logger.debug(f"Direct attempt failed for '{chosen_server.name}', using retry logic: {first_error}")
            
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
                _update_health_cache(chosen_server.id, True)
                backend_response = retry_result.result

                logger.info(
                    f"Successfully proxied to '{chosen_server.name}' "
                    f"after {retry_result.attempts} attempt(s) "
                    f"in {retry_result.total_duration_ms:.1f}ms"
                )

                # Check if streaming
                is_streaming = _is_streaming_response(backend_response)
                
                if is_streaming and log_id:
                    wrapped_response = _wrap_response_for_token_tracking(
                        backend_response, chosen_server, api_key_id, log_id, path
                    )
                    return wrapped_response, chosen_server
                else:
                    if log_id and backend_response.status_code == 200:
                        try:
                            body = await backend_response.aread()
                            if body:
                                data = json.loads(body.decode('utf-8'))
                                tokens = _extract_tokens_from_chunk(data)
                                if tokens.get("total_tokens") is not None or tokens.get("prompt_tokens") is not None:
                                    asyncio.create_task(_update_log_with_tokens_async(
                                        log_id,
                                        tokens["prompt_tokens"],
                                        tokens["completion_tokens"],
                                        tokens["total_tokens"]
                                    ))
                            return Response(
                                content=body,
                                status_code=backend_response.status_code,
                                headers=dict(backend_response.headers)
                            ), chosen_server
                        except Exception:
                            pass
                    return backend_response, chosen_server
            else:
                _update_health_cache(chosen_server.id, False)
                logger.warning(
                    f"Server '{chosen_server.name}' failed after {retry_result.attempts} "
                    f"attempts. Trying next server if available."
                )

        candidate_servers = [s for s in candidate_servers if s.id != chosen_server.id]
        if not candidate_servers:
            logger.error("No more candidate servers after Ollama failure")
            break
        current_index = safe_index % max(1, len(candidate_servers))

    logger.error(
        f"All {len(servers_tried)} backend server(s) failed after retries. "
        f"Servers tried: {', '.join(servers_tried)}"
    )
    raise HTTPException(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        detail=f"All backend servers unavailable. Tried: {', '.join(servers_tried)}"
    )


def _wrap_response_for_token_tracking(
    backend_response: Response,
    server: OllamaServer,
    api_key_id: Optional[int] = None,
    log_id: Optional[int] = None,
    path: str = ""
) -> StreamingResponse:
    """Wraps a streaming response to capture token usage from chunks."""
    
    async def token_tracking_stream():
        buffer = ""
        accumulated_tokens = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        tokens_finalized = False
        
        try:
            async for chunk in backend_response.aiter_raw():
                try:
                    chunk_text = chunk.decode('utf-8')
                except UnicodeDecodeError:
                    yield chunk
                    continue
                
                # CRITICAL: Yield the original chunk immediately to prevent hanging
                # But also process it for token tracking
                yield chunk
                
                # Process for token tracking (after yielding to not block)
                buffer += chunk_text
                
                # Process complete lines
                lines = buffer.split('\n')
                buffer = lines.pop() if buffer and not chunk_text.endswith('\n') else ""
                
                for line in lines:
                    if not line.strip():
                        continue
                    
                    # Try to parse as JSON (Ollama format)
                    try:
                        data_str = line
                        if line.startswith('data: '):
                            data_str = line[6:]
                            if data_str == '[DONE]':
                                continue
                        
                        data = json.loads(data_str)
                        
                        # Extract tokens from this chunk
                        chunk_tokens = _extract_tokens_from_chunk(data)
                        
                        # Update accumulated tokens (prefer non-None values)
                        for key in accumulated_tokens:
                            if chunk_tokens.get(key) is not None:
                                accumulated_tokens[key] = chunk_tokens[key]
                        
                        # If this is the final chunk, update the log
                        if data.get("done") and log_id and not tokens_finalized:
                            tokens_finalized = True
                            # Fire-and-forget token update
                            asyncio.create_task(_update_log_with_tokens_async(
                                log_id,
                                accumulated_tokens["prompt_tokens"],
                                accumulated_tokens["completion_tokens"],
                                accumulated_tokens["total_tokens"]
                            ))
                        
                    except json.JSONDecodeError:
                        pass  # Not JSON, skip token extraction
            
            # Process any remaining buffer
            if buffer.strip():
                try:
                    data_str = buffer
                    if buffer.startswith('data: '):
                        data_str = buffer[6:]
                    if data_str and data_str != '[DONE]':
                        data = json.loads(data_str)
                        if data.get("done") and log_id and not tokens_finalized:
                            tokens_finalized = True
                            chunk_tokens = _extract_tokens_from_chunk(data)
                            for key in accumulated_tokens:
                                if chunk_tokens.get(key) is not None:
                                    accumulated_tokens[key] = chunk_tokens[key]
                            
                            asyncio.create_task(_update_log_with_tokens_async(
                                log_id,
                                accumulated_tokens["prompt_tokens"],
                                accumulated_tokens["completion_tokens"],
                                accumulated_tokens["total_tokens"]
                            ))
                except json.JSONDecodeError:
                    pass
                
        except Exception as e:
            logger.error(f"Error in token tracking stream: {e}")
            # Don't re-raise, just stop processing tokens
    
    # Return StreamingResponse with proper headers
    response_headers = dict(backend_response.headers)
    # Remove content-length since we're streaming
    response_headers.pop('content-length', None)
    
    return StreamingResponse(
        token_tracking_stream(),
        status_code=backend_response.status_code,
        headers=response_headers,
        media_type=backend_response.headers.get('content-type', 'application/x-ndjson')
    )


def _is_streaming_response(response: Response) -> bool:
    """Check if a response is streaming based on headers."""
    content_type = response.headers.get('content-type', '')
    transfer_encoding = response.headers.get('transfer-encoding', '')
    
    if 'text/event-stream' in content_type:
        return True
    if 'chunked' in transfer_encoding.lower():
        return True
    if 'application/x-ndjson' in content_type:
        return True
    
    return False


async def _proxy_to_vllm(
    request: Request,
    server: OllamaServer,
    path: str,
    body_bytes: bytes,
    api_key_id: Optional[int] = None,
    log_id: Optional[int] = None
) -> Response:
    """
    Handles proxying a request to a vLLM server with token tracking.
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
                accumulated_tokens = {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                }
                tokens_finalized = False
                
                async with http_client.stream("POST", backend_url, json=vllm_payload, timeout=600.0, headers=headers) as vllm_response:
                    if vllm_response.status_code != 200:
                        error_body = await vllm_response.aread()
                        logger.error(f"vLLM server error ({vllm_response.status_code}): {error_body.decode()}")
                        error_chunk = {"error": f"vLLM server error: {error_body.decode()}"}
                        yield (json.dumps(error_chunk) + '\n').encode('utf-8')
                        return
                    
                    buffer = ""
                    async for chunk in vllm_response.aiter_raw():
                        try:
                            chunk_text = chunk.decode('utf-8')
                        except UnicodeDecodeError:
                            yield chunk
                            continue
                        
                        # CRITICAL: Yield immediately to prevent hanging
                        yield chunk
                        
                        # Process for token tracking
                        buffer += chunk_text
                        lines = buffer.split('\n')
                        buffer = lines.pop() if buffer and not chunk_text.endswith('\n') else ""
                        
                        for line in lines:
                            if not line.strip():
                                continue
                                
                            # Check for SSE data prefix
                            data_content = line
                            if line.startswith('data: '):
                                data_content = line[6:]
                                
                            if data_content == '[DONE]':
                                continue
                            
                            try:
                                data = json.loads(data_content)
                                
                                # Extract usage info if present
                                if "usage" in data and data["usage"]:
                                    usage = data["usage"]
                                    accumulated_tokens["prompt_tokens"] = usage.get("prompt_tokens")
                                    accumulated_tokens["completion_tokens"] = usage.get("completion_tokens")
                                    accumulated_tokens["total_tokens"] = usage.get("total_tokens")
                                
                                # Check for done signal
                                choices = data.get("choices", [])
                                if choices and choices[0].get("finish_reason"):
                                    if log_id and not tokens_finalized:
                                        tokens_finalized = True
                                        asyncio.create_task(_update_log_with_tokens_async(
                                            log_id,
                                            accumulated_tokens["prompt_tokens"],
                                            accumulated_tokens["completion_tokens"],
                                            accumulated_tokens["total_tokens"]
                                        ))
                            except json.JSONDecodeError:
                                pass
            
            return StreamingResponse(
                stream_generator(),
                media_type="application/x-ndjson",
                headers={'content-type': 'application/x-ndjson'}
            )
        else: # Non-streaming
            response = await http_client.post(backend_url, json=vllm_payload, timeout=600.0, headers=headers)
            response.raise_for_status()
            vllm_data = response.json()
            
            # Extract and log tokens for non-streaming response
            if log_id:
                usage = vllm_data.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
                total_tokens = usage.get("total_tokens")
                
                asyncio.create_task(_update_log_with_tokens_async(
                    log_id,
                    prompt_tokens, completion_tokens, total_tokens
                ))
            
            if path == "embeddings":
                ollama_data = translate_vllm_to_ollama_embeddings(vllm_data)
                return JSONResponse(content=ollama_data)
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
    Aggregates models from all configured backends.
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
                if "model" not in model:
                    model["model"] = model["name"]
                all_models[model['name']] = model
                model_count_on_server += 1
            else:
                logger.warning(f"/tags: Invalid model format found for server '{server.name}': {model}")
        
        logger.info(f"/tags: Added {model_count_on_server} models from server '{server.name}'")

    logger.info(f"/tags: Total unique models before adding 'auto': {len(all_models)}")

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

    try:
        asyncio.create_task(_async_log_usage(db, api_key.id, "/api/tags", 200, None, None))
    except Exception as e:
        logger.debug(f"Failed to queue usage log: {e}")
    
    final_model_list = list(all_models.values())
    logger.info("--- /tags endpoint: Finished model federation ---")

    return {"models": final_model_list}


async def _async_log_usage(
    db: AsyncSession, 
    api_key_id: int, 
    endpoint: str, 
    status_code: int, 
    server_id: Optional[int], 
    model: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None
) -> Optional[int]:
    """
    Fire-and-forget usage logging to avoid blocking responses.
    Returns the log ID if created.
    """
    try:
        from app.database.session import AsyncSessionLocal
        async with AsyncSessionLocal() as async_db:
            log_entry = await log_crud.create_usage_log(
                db=async_db,
                api_key_id=api_key_id,
                endpoint=endpoint,
                status_code=status_code,
                server_id=server_id,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens
            )
            return log_entry.id
    except Exception as e:
        logger.debug(f"Async usage logging failed: {e}")
        return None


async def _select_auto_model(db: AsyncSession, body: Dict[str, Any]) -> Optional[str]:
    """Selects the best model based on metadata and request content."""
    
    has_images = "images" in body and body["images"]
    
    prompt_content = ""
    if "prompt" in body:
        prompt_content = body["prompt"]
    elif "messages" in body:
        last_message = body["messages"][-1] if body["messages"] else {}
        if isinstance(last_message.get("content"), str):
            prompt_content = last_message["content"]
        elif isinstance(last_message.get("content"), list):
             text_part = next((p.get("text", "") for p in last_message["content"] if p.get("type") == "text"), "")
             prompt_content = text_part

    code_keywords = ["def ", "class ", "import ", "const ", "let ", "var ", "function ", "public static void", "int main("]
    contains_code = any(kw.lower() in prompt_content.lower() for kw in code_keywords)

    all_metadata = await model_metadata_crud.get_all_metadata(db)
    all_available_models = await server_crud.get_all_available_model_names(db)
    available_metadata = [m for m in all_metadata if m.model_name in all_available_models]

    if not available_metadata:
        logger.warning("Auto-routing failed: No models with metadata are available on active servers.")
        return None

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
    A catch-all route that proxies all other requests to the backend with token tracking.
    """
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

    # Handle 'think' parameter
    if model_name and isinstance(body, dict) and "think" in body:
        model_name_lower = model_name.lower()
        supported_think_models = ["qwen", "gpt-oss", "deepseek"]
        
        is_supported = any(keyword in model_name_lower for keyword in supported_think_models)

        if is_supported:
            if "gpt-oss" in model_name_lower and body.get("think") is True:
                logger.info(f"Translating 'think: true' to 'think: \"medium\"' for GPT-OSS model '{model_name}'")
                body["think"] = "medium"
                body_bytes = json.dumps(body).encode('utf-8')
        else:
            logger.warning(f"Model '{model_name}' is not in the known list for 'think' support. Removing 'think' parameter.")
            del body["think"]
            body_bytes = json.dumps(body).encode('utf-8')
            
    # Handle 'auto' model routing
    if model_name == "auto":
        chosen_model_name = await _select_auto_model(db, body)
        if not chosen_model_name:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auto-routing could not find an available and suitable model."
            )
        model_name = chosen_model_name
        body["model"] = model_name
        body_bytes = json.dumps(body).encode('utf-8')

    logger.info(f"proxy_ollama: Received {len(servers)} server(s) from get_active_servers dependency: {[s.name for s in servers]}")
    
    candidate_servers = servers
    if model_name:
        logger.info(f"proxy_ollama: Looking for servers with model '{model_name}'")
        servers_with_model = await server_crud.get_servers_with_model(db, model_name)

        if servers_with_model:
            candidate_servers = servers_with_model
            logger.info(f"Smart routing: Found {len(servers_with_model)} server(s) with model '{model_name}': {[s.name for s in servers_with_model]}")
        else:
            logger.warning(
                f"Model '{model_name}' not found in any server's catalog. "
                f"Falling back to round-robin across all {len(servers)} active server(s)."
            )

    if not candidate_servers:
        logger.error(f"proxy_ollama: No candidate servers available for model '{model_name}'")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No servers available for model '{model_name}'. Please check server status and model availability."
        )

    # Create initial usage log entry (without tokens - will be updated later for streaming)
    is_token_trackable_endpoint = path in ("generate", "chat", "embeddings")
    
    log_id = None
    if is_token_trackable_endpoint:
        log_id = await _async_log_usage(
            db, api_key.id, f"/api/{path}", 200, None, model_name,
            None, None, None
        )
    
    # Proxy to one of the candidate servers
    response, chosen_server = await _reverse_proxy(
        request, path, candidate_servers, body_bytes,
        api_key_id=api_key.id, log_id=log_id
    )

    # Update log with server_id if we have a log entry
    if log_id and chosen_server:
        try:
            from app.database.session import AsyncSessionLocal
            async with AsyncSessionLocal() as async_db:
                from sqlalchemy import update
                from app.database.models import UsageLog
                await async_db.execute(
                    update(UsageLog).where(UsageLog.id == log_id).values(server_id=chosen_server.id)
                )
                await async_db.commit()
        except Exception as e:
            logger.debug(f"Failed to update server_id for log {log_id}: {e}")

    # For non-streaming, non-tracked endpoints, log without tokens
    if not is_token_trackable_endpoint:
        try:
            asyncio.create_task(_async_log_usage(
                db, api_key.id, f"/api/{path}", response.status_code, chosen_server.id, model_name
            ))
        except Exception as e:
            logger.debug(f"Failed to queue usage log: {e}")

    return response
