import asyncio
import json
import logging
import datetime
from typing import List, Tuple, Optional, Dict, Any
from fastapi import APIRouter, Depends, Request, Response, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.api.v1.dependencies import get_valid_api_key, rate_limiter, ip_filter, get_settings
from app.database.models import APIKey, OllamaServer
from app.crud import log_crud, server_crud, model_metadata_crud
from app.core.retry import retry_with_backoff, RetryConfig
from app.core.events import event_manager, ProxyEvent
from app.schema.settings import AppSettingsModel
import secrets
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


async def _extract_image_descriptions(request: Request, db: AsyncSession, vision_model: str, images: list, prompt_text: str, http_client: httpx.AsyncClient, sender: str = "anon") -> str:
    """Helper to offload images to a VLM and get a text description back."""
    user_query = prompt_text
    vision_prompt = (
        "Analyze the provided images and describe their contents in detail. "
        f"Pay special attention to elements relevant to this user query:\n\"{user_query}\"\n\n"
        "Be thorough but concise."
    )
    
    vlm_payload = {
        "model": vision_model,
        "messages":[
            {
                "role": "user",
                "content": vision_prompt,
                "images": images
            }
        ],
        "stream": False,
        "options": {"temperature": 0.3}
    }
    
    image_descriptions = "[Image Analysis Failed]"
    from app.database.session import AsyncSessionLocal
    
    async with AsyncSessionLocal() as v_db:
        v_servers = await server_crud.get_servers_with_model(v_db, vision_model)
        
    if v_servers:
        try:
            # We briefly disable strict context for the internal VLM request
            old_strict = getattr(request.state, 'enforce_strict_context', False)
            request.state.enforce_strict_context = False
            
            resp, _ = await _reverse_proxy(request, "chat", v_servers, json.dumps(vlm_payload).encode(), sender=sender, is_subrequest=True)
            
            if hasattr(resp, 'body'):
                v_data = json.loads(resp.body.decode())
                image_descriptions = v_data.get("message", {}).get("content", "").strip()
            
            request.state.enforce_strict_context = old_strict
        except Exception as e:
            logger.error(f"Vision processor subrequest failed: {e}")
    else:
        image_descriptions = "[Vision processor model is offline or unavailable]"
        
    return image_descriptions

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
    http_client: httpx.AsyncClient,
    server: OllamaServer,
    path: str,
    method: str,
    headers: dict,
    query_params,
    body_bytes: bytes,
    request_id: Optional[str] = None,
    model: str = "unknown",
    sender: str = "anon"
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
        if request_id:
            event_manager.emit(ProxyEvent(
                event_type="assigned", request_id=request_id, 
                model=model, server=server.name, sender=sender
            ))

        backend_response = await http_client.send(backend_request, stream=True)

        if backend_response.status_code >= 400:
            # ERROR DIAGNOSTICS: Read the body to see WHY it failed
            error_body = await backend_response.aread()
            error_text = error_body.decode('utf-8', errors='replace')
            
            # Log the deep details
            logger.error(f"--- BACKEND FAILURE DIAGNOSTICS ---")
            logger.error(f"Server: {server.name} ({server.url})")
            logger.error(f"Status: {backend_response.status_code}")
            logger.error(f"Error Body: {error_text[:500]}")
            try:
                sent_body = json.loads(body_bytes)
                if "model" in sent_body: logger.error(f"Target Model: {sent_body['model']}")
                
                # --- ENHANCED LOGGING (Requested) ---
                if "options" in sent_body:
                    logger.error(f"Sent Options Content: {json.dumps(sent_body['options'])}")
                if "tools" in sent_body:
                    logger.error(f"Sent Tools Content: {json.dumps(sent_body['tools'])}")
                if "messages" in sent_body:
                    logger.error(f"Message Count: {len(sent_body['messages'])}")
            except: pass
            logger.error(f"----------------------------------")

            if backend_response.status_code in (429, 503):
                raise httpx.HTTPStatusError(f"Backend Busy", request=backend_request, response=backend_response)
            
            raise httpx.HTTPStatusError(f"Backend Error: {error_text[:100]}", request=backend_request, response=backend_response)

        return backend_response

    except Exception as e:
        if not isinstance(e, httpx.HTTPStatusError):
            logger.error(f"Network/Connection Error to {server.name}: {str(e)}")
        raise


def _normalize_payload_for_ollama(body_bytes: bytes, max_context_limit: int = 32768, enforce_strict: bool = False) -> bytes:
    """
    Ensures payload is compatible with Ollama backends.
    1. Converts OpenAI-style multi-part messages to Ollama string+images format.
    2. Handles context size: preserves user's choice for raw models, enforces metadata for bundles.
    
    Args:
        body_bytes: The raw JSON payload
        max_context_limit: The maximum context window from model metadata
        enforce_strict: If True, always use max_context_limit (for bundles/orchestrators).
                       If False, only clamp if user exceeded limit (for raw models).
    """
    if not body_bytes:
        return body_bytes
    try:
        temp_body = json.loads(body_bytes)
        modified = False
        
        # Initialize options dict if missing
        if "options" not in temp_body:
            temp_body["options"] = {}
            
        requested_ctx = temp_body["options"].get("num_ctx")
        
        try:
            req_ctx_int = int(requested_ctx) if requested_ctx is not None else None
        except ValueError:
            req_ctx_int = None
            
        # KV CACHE PRE-ALLOCATION FIX:
        # To force Ollama to allocate a maxed-out KV cache once and for all,
        # we consistently inject the max_context_limit into every request.
        # This causes a long TTFT on the very first prompt, but allows 
        # lightning-fast TPS on all subsequent prompts because Ollama 
        # reuses the pre-allocated VRAM instead of reloading the model.
        if req_ctx_int != max_context_limit:
            temp_body["options"]["num_ctx"] = max_context_limit
            modified = True
            logger.info(f"Enforcing max KV cache allocation: {max_context_limit} (was {req_ctx_int})")

        # --- 2. Message Format Normalization ---
        if "messages" in temp_body:
            for msg in temp_body["messages"]:
                    if isinstance(msg.get("content"), list):
                        text_parts = []
                        images = []
                        for part in msg["content"]:
                            if isinstance(part, dict):
                                if part.get("type") == "text":
                                    text_parts.append(part.get("text", ""))
                                elif part.get("type") == "image_url":
                                    url = part.get("image_url", {}).get("url", "")
                                    if url.startswith("data:"):
                                        try:
                                            base64_data = url.split(",", 1)[1]
                                            if base64_data:
                                                images.append(base64_data)
                                        except (IndexError, AttributeError):
                                            pass
                                    else:
                                        images.append(url)
                        msg["content"] = "\n".join(text_parts)
                        if images:
                            msg["images"] = images
                        elif "images" in msg:
                            del msg["images"]
                        modified = True

                    # 2. Clean 'data:image' prefix from base64 strings
                    if "images" in msg and isinstance(msg["images"], list):
                        cleaned_images = []
                        for img in msg["images"]:
                            if isinstance(img, str) and img.startswith("data:"):
                                try:
                                    img = img.split(",", 1)[1]
                                    modified = True
                                except IndexError:
                                    pass
                            if img:
                                cleaned_images.append(img)
                        
                        msg["images"] = cleaned_images
                        if not msg["images"]:
                            del msg["images"]
                            modified = True

        # Always return the modified payload if we touched options/context/images
        if modified:
            return json.dumps(temp_body).encode('utf-8')
            
    except Exception as e:
        logger.warning(f"Payload normalization failed: {e}", exc_info=True)
        
    return body_bytes


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
                        api_key_id: Optional[int] = None, log_id: Optional[int] = None,
                        request_id: Optional[str] = None, model: str = "unknown",
                        sender: str = "system", is_subrequest: bool = False,
                        client_wants_stream: bool = True) -> Tuple[Response, OllamaServer]:
    """
    Core lollms hub reverse proxy logic with retry support and token tracking.
    """
    http_client: httpx.AsyncClient = request.app.state.http_client
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

    # Retrieve model metadata to determine physical context limit for clamping
    from app.crud.model_metadata_crud import get_metadata_by_model_name
    from app.database.session import AsyncSessionLocal
    
    model_limit = 32768
    async with AsyncSessionLocal() as meta_db:
        # Check metadata for the resolved physical model
        meta = await get_metadata_by_model_name(meta_db, model)
        if meta:
            model_limit = meta.max_context

    servers_tried = []

    for server_attempt in range(len(candidate_servers)):
        safe_index = (current_index + server_attempt) % len(candidate_servers)
        chosen_server = candidate_servers[safe_index]
        
        logger.info(f"Server attempt {server_attempt + 1}/{len(candidate_servers)}: selected '{chosen_server.name}' at index {safe_index} (Limit: {model_limit})")

        servers_tried.append(chosen_server.name)

        # --- RE-ENCODING FOR OLLAMA ---
        local_body_bytes = body_bytes
        if chosen_server.server_type == 'ollama':
            # Use strict context enforcement for bundles/orchestrators, lenient for raw models
            is_strict = getattr(request.state, 'enforce_strict_context', False)
            local_body_bytes = _normalize_payload_for_ollama(
                body_bytes, 
                max_context_limit=model_limit,
                enforce_strict=is_strict
            )

        if chosen_server.server_type == 'vllm':
            logger.info(f"Using vLLM branch for server '{chosen_server.name}'")
            try:
                response = await _proxy_to_vllm(request, chosen_server, path, body_bytes, api_key_id, log_id, request_id, model, sender=sender)
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
        
        # DEBUG: Log the actual payload being sent
        try:
            debug_body = json.loads(local_body_bytes)
            has_images = any(isinstance(m.get("content"), list) and 
                           any(p.get("type") == "image_url" for p in m.get("content", []))
                           for m in debug_body.get("messages", []))
            logger.info(f"DEBUG: Payload has images in original format: {has_images}")
            for i, m in enumerate(debug_body.get("messages", [])):
                if isinstance(m.get("content"), list):
                    logger.info(f"DEBUG: Message {i} has list content with {len(m['content'])} parts")
                    for j, part in enumerate(m["content"]):
                        logger.info(f"  Part {j}: type={part.get('type')}, has_image_url={bool(part.get('image_url', {}).get('url', '')[:50])}")
                elif isinstance(m.get("content"), str):
                    logger.info(f"DEBUG: Message {i} has string content: {m.get('content', '')[:100]}...")
                logger.info(f"  Message {i} has images key: {'images' in m}, images length: {len(m.get('images', []))}")
        except Exception as e:
            logger.warning(f"DEBUG: Could not parse body for debug: {e}")
        
        first_attempt_start = asyncio.get_event_loop().time()
        
        try:
            backend_response = await _send_backend_request(
                http_client=http_client,
                server=chosen_server,
                path=path,
                method=request.method,
                headers=headers,
                query_params=request.query_params,
                body_bytes=local_body_bytes,
                request_id=request_id,
                model=model
            )
            
            first_attempt_duration = asyncio.get_event_loop().time() - first_attempt_start
            
            _update_health_cache(chosen_server.id, True)
            
            # Check if this is a streaming response
            is_streaming = _is_streaming_response(backend_response)
            
            # Use streaming wrapper ONLY if client requested stream AND backend supports it
            if is_streaming and client_wants_stream and log_id and not is_subrequest:
                # Wrap for token tracking and live visualization
                wrapped_response = _wrap_response_for_token_tracking(
                    backend_response, chosen_server, api_key_id, log_id, path, request_id, model, sender
                )
                return wrapped_response, chosen_server
            else:
                # Non-streaming, return as Starlette Response.
                try:
                    raw_body = await backend_response.aread()
                    decoded_body = raw_body.decode('utf-8')
                    
                    # DEFENSIVE FIX: If the backend returned NDJSON (multiple objects), 
                    # we must parse them and return only the final one to the client.
                    lines = [line.strip() for line in decoded_body.split('\n') if line.strip()]
                    
                    final_data = {}
                    full_content = ""
                    
                    for line in lines:
                        try:
                            chunk = json.loads(line)
                            if "message" in chunk:
                                full_content += chunk["message"].get("content", "")
                            elif "response" in chunk:
                                full_content += chunk.get("response", "")
                            
                            # Use the last 'done' chunk as the base for metadata
                            if chunk.get("done") or not final_data:
                                final_data = chunk
                        except json.JSONDecodeError:
                            continue
                    
                    # Ensure the final object has the total aggregated text
                    if "message" in final_data:
                        final_data["message"]["content"] = full_content
                    elif "response" in final_data:
                        final_data["response"] = full_content

                    # Extract tokens for logging
                    if log_id and backend_response.status_code == 200 and final_data:
                        tokens = _extract_tokens_from_chunk(final_data)
                        if tokens.get("total_tokens") is not None or tokens.get("prompt_tokens") is not None:
                            asyncio.create_task(_update_log_with_tokens_async(
                                log_id,
                                tokens["prompt_tokens"],
                                tokens["completion_tokens"],
                                tokens["total_tokens"]
                            ))
                    
                    # Return a single valid JSON object to satisfy the client library
                    # We strip Content-Length and Transfer-Encoding because JSONResponse recalculates them
                    resp_headers = {k: v for k, v in backend_response.headers.items() 
                                   if k.lower() not in ('content-length', 'transfer-encoding', 'content-encoding')}
                    return JSONResponse(
                        content=final_data,
                        status_code=backend_response.status_code,
                        headers=resp_headers
                    ), chosen_server
                except Exception as e:
                    logger.error(f"Failed to process backend response: {e}")
                    raise
            
        except (Exception, httpx.HTTPStatusError) as first_error:
            resp = getattr(first_error, 'response', None)
            status_code = resp.status_code if resp else None
            
            is_busy = status_code in (429, 503)
            is_server_error = status_code == 500 # Strict check for internal logic crashes
            
            if is_busy:
                # FOR BUSY (503): High patience, wait and retry.
                if request_id:
                    event_manager.emit(ProxyEvent(
                        event_type="received", request_id=request_id, 
                        model=model, server=chosen_server.name,
                        error_message="Server Busy - Waiting for slot..."
                    ))
                await asyncio.sleep(2)
            elif is_server_error:
                # FOR SERVER ERROR (500): Low patience. Fail fast so user sees the error.
                logger.error(f"Fatal Backend Error from {chosen_server.name}. Logic error or malformed request. Skipping retries.")
                # We don't mark it 'False' (Dead) because the server is alive, it just didn't like THIS request.
                break 
            else:
                # For network timeouts/connection refused: Mark as DEAD.
                _update_health_cache(chosen_server.id, False)
            
            logger.warning(f"Attempt failed for '{chosen_server.name}' (Busy: {is_busy}, SrvErr: {is_server_error}). Error: {first_error}")
            
            retry_result = await retry_with_backoff(
                _send_backend_request,
                http_client=http_client,
                server=chosen_server,
                path=path,
                method=request.method,
                headers=headers,
                query_params=request.query_params,
                body_bytes=local_body_bytes,
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
    if request_id:
        event_manager.emit(ProxyEvent("error", request_id, model, "none", sender))

    raise HTTPException(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        detail=f"All backend servers unavailable. Tried: {', '.join(servers_tried)}"
    )


def _wrap_response_for_token_tracking(
    backend_response: Response,
    server: OllamaServer,
    api_key_id: Optional[int] = None,
    log_id: Optional[int] = None,
    path: str = "",
    request_id: Optional[str] = None,
    model: str = "unknown",
    sender: str = "anon"
) -> StreamingResponse:
    """Wraps a streaming response to capture token usage from chunks."""
    
    async def token_tracking_stream():
        buffer = ""
        is_first_token = True
        start_time = asyncio.get_event_loop().time()
        first_token_time = 0.0
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
                # This ensures the lowest possible TTFT for the client.
                yield chunk
                
                if is_first_token and request_id:
                    is_first_token = False
                    first_token_time = asyncio.get_event_loop().time()
                    ttft_ms = (first_token_time - start_time) * 1000
                    event_manager.emit(ProxyEvent(
                        event_type="active", 
                        request_id=request_id, 
                        model=model, 
                        server=server.name, 
                        sender=sender,
                        ttft=round(ttft_ms, 1)
                    ))

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
            
            # Emit event: request finished
            if request_id:
                total_duration = asyncio.get_event_loop().time() - (first_token_time or start_time)
                tps = (accumulated_tokens["completion_tokens"] or 0) / max(total_duration, 0.001)
                
                event_manager.emit(ProxyEvent(
                    event_type="completed", 
                    request_id=request_id, 
                    model=model, 
                    server=server.name, 
                    sender=sender,
                    token_count=accumulated_tokens["total_tokens"] or 0,
                    tps=round(tps, 2)
                ))

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
            # Check if this was a normal disconnect or an actual error
            is_disconnect = any(x in str(e).lower() for x in ["broken pipe", "connection reset", "cancelled"])
            
            if is_disconnect and not is_first_token:
                # If we were already streaming, this is often just the client closing the socket
                logger.debug(f"Client disconnected for {request_id}. Marking as completed.")
                event_manager.emit(ProxyEvent("completed", request_id, model, server.name, sender, 
                                            token_count=accumulated_tokens["total_tokens"] or 0,
                                            error_message=None)) # Explicitly clear error
            else:
                error_detail = str(e)
                logger.error(f"Error in token tracking stream for {request_id}: {error_detail}")
                event_manager.emit(ProxyEvent("error", request_id, model, server.name, sender, error_message=error_detail))
        finally:
            # SAFETY CHECK: Ensure the UI always gets a closing event
            if not tokens_finalized and request_id:
                event_manager.emit(ProxyEvent(
                    event_type="completed", 
                    request_id=request_id, 
                    model=model, 
                    server=server.name, 
                    sender=sender,
                    token_count=accumulated_tokens["total_tokens"] or 0,
                    prompt_tokens=accumulated_tokens["prompt_tokens"] or p_tokens,
                    tps=0
                ))    


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
    log_id: Optional[int] = None,
    request_id: Optional[str] = None,
    model: str = "unknown",
    sender: str = "system"
) -> Response:
    """
    Handles proxying a request to a vLLM server with token tracking.
    """
    http_client: httpx.AsyncClient = request.app.state.http_client
    
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
                is_first_token = True
                
                # vLLM connect timeout fix
                timeout = httpx.Timeout(read=600.0, write=600.0, connect=2.0, pool=10.0)
                async with http_client.stream("POST", backend_url, json=vllm_payload, timeout=timeout, headers=headers) as vllm_response:
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

                        if is_first_token and request_id:
                            is_first_token = False
                            event_manager.emit(ProxyEvent("active", request_id, model, server.name, sender=sender))
                        
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
            
            # Implementation for vLLM chat non-streaming
            choices = vllm_data.get("choices", [])
            content = choices[0].get("message", {}).get("content", "") if choices else ""
            
            ollama_compatible = {
                "model": model_name,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                "message": {"role": "assistant", "content": content},
                "done": True,
                "total_duration": 0, # vLLM doesn't easily provide these in this format
                "load_duration": 0,
                "prompt_eval_count": vllm_data.get("usage", {}).get("prompt_tokens", 0),
                "eval_count": vllm_data.get("usage", {}).get("completion_tokens", 0)
            }
            return JSONResponse(content=ollama_compatible)

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

    # Add Pools, Bundles, and Vision Augmenters to the list
    try:
        from app.database.models import EnsembleOrchestrator, SmartRouter, VisionAugmenter
        
        # Add Pools
        res_p = await db.execute(select(SmartRouter).filter(SmartRouter.is_active == True))
        for p in res_p.scalars().all():
            all_models[p.name] = {
                "name": p.name, "model": p.name, "size": 0, "digest": f"pool-{p.id}",
                "details": {"format": "pool", "family": "router", "parameter_size": "pool"}
            }

        # Add Bundles
        try:
            result = await db.execute(select(EnsembleOrchestrator))
            bundles =[b for b in result.scalars().all() if getattr(b, 'is_active', True)]
        except Exception:
            bundles =[]
        for b in bundles:
            all_models[b.name] = {
                "name": b.name,
                "model": b.name,
                "modified_at": b.created_at.isoformat() + "Z",
                "size": 0,
                "digest": f"bundle-{b.id}",
                "details": {
                    "parent_model": "",
                    "format": "bundle",
                    "family": "ensemble",
                    "families": ["ensemble"],
                    "parameter_size": "multiple",
                    "quantization_level": "N/A"
                }
            }

        # Add Workflows
        try:
            result_w = await db.execute(select(Workflow).filter(Workflow.is_active == True))
            for w in result_w.scalars().all():
                all_models[w.name] = {
                    "name": w.name, "model": w.name, "size": 0, "digest": f"flow-{w.id}",
                    "details": {"format": "workflow", "family": "graph", "parameter_size": "multi-node"}
                }
        except Exception:
            pass
            
        # Add Vision Augmenters
        try:
            result_v = await db.execute(select(VisionAugmenter).filter(VisionAugmenter.is_active == True))
            for v in result_v.scalars().all():
                all_models[v.name] = {
                    "name": v.name, "model": v.name, "size": 0, "digest": f"vision-{v.id}",
                    "details": {"format": "augmenter", "family": "vision", "parameter_size": "pipeline"}
                }
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Failed to load virtual proxy models for /tags: {e}")

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


async def _select_auto_model(db: AsyncSession, body: Dict[str, Any], overrides: Dict[str, bool] = None) -> Optional[str]:
    """Selects the best model based on metadata, request content, and context length."""
    if overrides is None: overrides = {}
    
    has_images = overrides.get("require_vision", False) or ("images" in body and body["images"])
    
    # 1. Extract and Analyze Content
    full_history_text = ""
    last_user_prompt = ""
    
    if "prompt" in body:
        last_user_prompt = body["prompt"]
        full_history_text = last_user_prompt
    elif "messages" in body:
        for msg in body["messages"]:
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join([p.get("text", "") for p in content if p.get("type") == "text"])
            full_history_text += content + " "
            if msg.get("role") == "user":
                last_user_prompt = content

    # Detect Reasoning Intent
    reasoning_keywords =["solve", "prove", "math", "why", "logic", "calculate", "step by step", "complex"]
    is_reasoning_task = overrides.get("prefer_reasoning", False) or any(kw in last_user_prompt.lower() for kw in reasoning_keywords)

    # Detect Coding Intent
    code_keywords =["def ", "class ", "import ", "const ", "let ", "var ", "function ", "public static void", "int main("]
    contains_code = overrides.get("prefer_code", False) or any(kw.lower() in last_user_prompt.lower() for kw in code_keywords)

    # Estimate Context Length (approx 4 chars per token)
    estimated_tokens = len(full_history_text) // 4

    # 2. Filter Candidates
    all_metadata = await model_metadata_crud.get_all_metadata(db)
    all_available_models = await server_crud.get_all_available_model_names(db)
    available_metadata = [m for m in all_metadata if m.model_name in all_available_models]

    if not available_metadata:
        logger.warning("Auto-routing failed: No models with metadata are available on active servers.")
        return None

    candidate_models = available_metadata

    # Tier 1: Capability Matching
    if has_images:
        # Strict filter: ONLY models that support images
        candidate_models = [m for m in candidate_models if m.supports_images]
    else:
        # Soft filter: If it's a text task, prioritize text-only models 
        # (models that don't support images) to save the vision experts for vision tasks.
        text_only = [m for m in candidate_models if not m.supports_images]
        if text_only:
            candidate_models = text_only

    if is_reasoning_task:
        reasoning_models = [m for m in candidate_models if m.is_reasoning_model]
        if reasoning_models:
            candidate_models = reasoning_models

    if contains_code:
        code_models = [m for m in candidate_models if m.is_code_model]
        if code_models:
            candidate_models = code_models

    # Tier 2: Resource/Constraint Matching
    is_fast = overrides.get("prefer_fast", False) or body.get("options", {}).get("fast_model")
    if is_fast:
        fast_models = [m for m in candidate_models if m.is_fast_model]
        if fast_models:
            candidate_models = fast_models

    # Filter by context window (don't route huge prompts to tiny models)
    capable_context = [m for m in candidate_models if m.max_context >= estimated_tokens]
    if capable_context:
        candidate_models = capable_context

    if not candidate_models:
        logger.warning("Auto-routing: Strict criteria failed. Falling back to priority list.")
        candidate_models = available_metadata

    if not candidate_models:
        return None

    # Sort by priority
    candidate_models.sort(key=lambda x: x.priority)
    top_priority = candidate_models[0].priority
    
    # Tier 3: Load Balancing (Random choice among tied top-priority models)
    best_tier = [m for m in candidate_models if m.priority == top_priority]
    best_model = secrets.choice(best_tier)
    
    logger.info(f"Auto-routing: Detected [Tokens: ~{estimated_tokens}, Reasoning: {is_reasoning_task}, Code: {contains_code}]. Selected '{best_model.model_name}'.")
    return best_model.model_name


async def _resolve_target(db: AsyncSession, name: str, messages: List[Dict[str, Any]], depth: int = 0, request: Request = None) -> Tuple[str, List[Dict[str, Any]]]:
    """Recursively resolves a name into a physical model + final message list."""
    if depth > 10: return name, messages # Circular protection

    from app.database.models import VirtualAgent, SmartRouter, Workflow
    
    if request is not None:
        request.state.enforce_strict_context = True
    
    # 0. Check for Visual Workflow (Conception UI)
    wf_res = await db.execute(select(Workflow).filter(Workflow.name == name, Workflow.is_active == True))
    workflow = wf_res.scalars().first()
    if workflow:
        # GRAPH RESOLUTION ENGINE
        nodes = workflow.graph_data.get("nodes", [])
        
        # Logic: Find the terminal node (Output) and trace backward to the first LLM
        # GRAPH RESOLUTION ENGINE (DAG Crawler)
        # Find terminal Output node
        output_nodes = [n for n in nodes if n["type"] == "hub/output"]
        if not output_nodes:
            return await _resolve_target(db, "auto", messages, depth + 1, request=request)

        # Tracing Logic:
        # Find the LLM node connected to the output, then follow its inputs.
        def find_source_node(input_id, nodes):
            for n in nodes:
                if "outputs" in n:
                    for out in n["outputs"]:
                        if out.get("links") and input_id in out["links"]:
                            return n
            return None

        # GRAPH RESOLUTION ENGINE (DAG Crawler)
        # Find terminal Output node
        exit_node = next((n for n in nodes if n["type"] == "hub/output"), None)
        if not exit_node:
            logger.warning(f"Workflow '{name}' has no Output node.")
            return await _resolve_target(db, "auto", messages, depth + 1, request=request)

        def find_source_node_and_slot(input_link_id):
            for n in nodes:
                if "outputs" in n:
                    for idx, out in enumerate(n["outputs"]):
                        if out.get("links") and input_link_id in out["links"]:
                            return n, idx
            return None, 0

        async def execute_cognitive_path(link_id_or_name, history):
            """Executes a cognitive branch (Node or Sub-Workflow) and returns string response."""
            m_target = ""
            
            # CASE A: Connection from another node (Link ID)
            if isinstance(link_id_or_name, int):
                src_node, slot_idx = find_source_node_and_slot(link_id_or_name)
                if not src_node: return ""
                
                # If it's a logic node (Merger, Composer), resolve the string value
                if src_node["type"] not in ("hub/llm_chat", "hub/llm_instruct", "hub/model"):
                    return await resolve_node_value(src_node, slot_idx)
                
                # It's a cognitive node, get the model target
                props = src_node.get("properties", {})
                m_target = props.get("model", "auto")
            
            # CASE B: Literal Name (e.g. from a dropdown)
            else:
                m_target = str(link_id_or_name)

            # --- RECURSION CHECK: Is this target another workflow? ---
            # We look up the name in the workflow table
            wf_check = await db.execute(select(Workflow).filter(Workflow.name == m_target, Workflow.is_active == True))
            sub_workflow = wf_check.scalars().first()
            
            if sub_workflow:
                logger.info(f"[GraphRecursion] Entering sub-workflow: {m_target}")
                # Recursively resolve the entire sub-workflow
                # This uses the main _resolve_target logic but for a specific branch
                res_model, res_msgs = await _resolve_target(db, m_target, history, depth=depth+1, request=request)
                
                # Now execute the resulting model/messages
                servers = await server_crud.get_servers_with_model(db, res_model)
                if not servers: return f"[Error] Sub-workflow model '{res_model}' offline."
                
                resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": res_model, "messages": res_msgs, "stream": False}).encode(), is_subrequest=True)
                return json.loads(resp.body.decode()).get("message", {}).get("content", "") if hasattr(resp, 'body') else ""

            # --- STANDARD EXECUTION: Physical Model ---
            servers = await server_crud.get_servers_with_model(db, m_target)
            if not servers: return f"[Error] Expert Model '{m_target}' offline."
            
            resp, _ = await _reverse_proxy(
                request, "chat", servers,
                json.dumps({"model": m_target, "messages": history, "stream": False}).encode(),
                is_subrequest=True,
                sender="graph-moe-expert"
            )
            return json.loads(resp.body.decode()).get("message", {}).get("content", "") if hasattr(resp, 'body') else ""
            
            # For non-cognitive nodes (Composers, etc), just resolve value normally
            return await resolve_node_value(src_node, slot_idx)

        async def resolve_node_value(node, output_slot_idx=0):
            """Recursive solver for the graph data flow."""
            if not node: return None
            
            # 0. Logic: MOE (Parallel Synthesis)
            if node["type"] == "hub/moe":
                props = node.get("properties", {})
                
                # A. Resolve Context
                history = []
                if node.get("inputs") and node["inputs"][0].get("link"):
                    history = await resolve_node_value(*find_source_node_and_slot(node["inputs"][0]["link"]))
                
                # B. Expert Discovery (Identify names for status message)
                expert_tasks = []
                expert_names = []
                
                for i in range(1, len(node.get("inputs", []))):
                    link = node["inputs"][i].get("link")
                    if link:
                        # Find the name for the status message
                        src, _ = find_source_node_and_slot(link)
                        name = src.get("properties", {}).get("model", "Expert " + str(i))
                        expert_names.append(name)
                        expert_tasks.append(execute_cognitive_path(link, history))
                
                if not expert_tasks: return "No experts connected to MoE."
                
                # STATUS UPDATE: Tell user what models are being engaged
                if props.get("send_status_update"):
                    status_text = f"Engaging Expert Panel: {', '.join(expert_names)}..."
                    event_manager.emit(ProxyEvent("active", request_id, "MoE Block", "Gateway", sender, error_message=status_text))

                # C. Execution
                responses = await asyncio.gather(*expert_tasks, return_exceptions=True)
                
                # C. Assemble Panel for Orchestrator
                panel_data = ""
                for i, resp in enumerate(responses):
                    val = str(resp) if not isinstance(resp, Exception) else f"Error: {str(resp)}"
                    # Wrap in think tags if Monologue is requested
                    if props.get("show_monologue"):
                        val = f""
                    panel_data += f"### EXPERT {i+1} FEEDBACK:\n{val}\n\n"

                # D. Final Synthesis Call
                orchestrator_model = props.get("orchestrator", "auto")
                instructions = props.get("system_prompt", "")
                
                # PRESERVE PERSONA: Construct synthesis messages from the existing history
                # This ensures the 'Identity' injected earlier stays in the final step's buffer
                final_messages = list(history) if isinstance(history, list) else [{"role": "user", "content": str(history)}]
                
                # Append the expert panel and instructions as a meta-instruction for the orchestrator
                final_synth_msg = (
                    f"### EXPERT PANEL OUTPUTS\n{panel_data}\n\n"
                    f"### SYNTHESIS MANDATE\n{instructions}"
                )
                final_messages.append({"role": "user", "content": final_synth_msg})
                
                servers = await server_crud.get_servers_with_model(db, orchestrator_model)
                if not servers: return f"[Error] Orchestrator '{orchestrator_model}' offline."
                
                resp_obj, _ = await _reverse_proxy(
                    request, "chat", servers, 
                    json.dumps({
                        "model": orchestrator_model, 
                        "messages": final_messages,
                        "stream": False
                    }).encode(),
                    is_subrequest=True,
                    sender="graph-moe"
                )
                
                if hasattr(resp_obj, 'body'):
                    try:
                        data = json.loads(resp_obj.body.decode())
                        content = data.get("message", {}).get("content", "")
                        if not content:
                            logger.warning("[GraphMoE] Orchestrator returned empty string. Returning raw expert data.")
                            return f"The orchestrator returned no text. Here is the raw expert summary:\n\n{panel_data}"
                        return content
                    except Exception as e:
                        return f"JSON Error in Orchestrator: {str(e)}"
                return "Critical Synthesis Error: No response from compute node."

            # 1. Logic: Composer (Aggregates multiple text inputs)
            if node["type"] == "hub/system_composer":
                parts = []
                for inp in node.get("inputs",[]):
                    if inp.get("link") is not None:
                        n, idx = find_source_node_and_slot(inp["link"])
                        val = await resolve_node_value(n, idx)
                        if val: parts.append(str(val))
                return "\n\n".join(parts)

            # 1.1 Logic: System Merger (Identity + Categorized Skills)
            if node["type"] == "hub/system_merger":
                identity = ""
                skills = []
                
                # Input 0 is always Base Identity
                inputs = node.get("inputs", [])
                if inputs and inputs[0].get("link"):
                    identity = await resolve_node_value(*find_source_node_and_slot(inputs[0]["link"]))
                
                # Inputs 1+ are Skills
                for i in range(1, len(inputs)):
                    if inputs[i].get("link"):
                        skill_val = await resolve_node_value(*find_source_node_and_slot(inputs[i]["link"]))
                        if skill_val: skills.append(str(skill_val))
                
                merged = f"## Identity\n{identity}"
                if skills:
                    merged += "\n\n## Capabilities & Skills\n" + "\n\n".join(skills)
                return merged

            # 2. Logic: Skill/Personality/String Provider
            if node["type"] == "hub/skill":
                from app.core.skills_manager import SkillsManager
                skill_name = node.get("properties", {}).get("name")
                skills = SkillsManager.get_all_skills()
                skill = next((s for s in skills if s["name"] == skill_name), None)
                return skill["raw"] if skill else ""
                
            if node["type"] == "hub/personality":
                from app.core.personalities_manager import PersonalityManager
                import re
                p_name = node.get("properties", {}).get("name")
                personalities = PersonalityManager.get_all_personalities()
                p = next((x for x in personalities if x["name"] == p_name), None)
                if not p: return ""
                
                # SLOT 0: System Prompt (Body ONLY)
                if output_slot_idx == 0:
                    raw = p["raw"]
                    # Strip YAML Frontmatter (--- ... ---) to ensure the LLM sees a clean prompt
                    body = re.sub(r'^---\s*\n.*?\n---\s*\n', '', raw, flags=re.DOTALL).strip()
                    return body
                return ""

            # 3. Transformer: Extract Text from Messages
            if node["type"] == "hub/extract_text":
                if node.get("inputs") and len(node["inputs"]) > 0 and node["inputs"][0].get("link"):
                    n, idx = find_source_node_and_slot(node["inputs"][0]["link"])
                    msgs = await resolve_node_value(n, idx)
                    if msgs and isinstance(msgs, list) and len(msgs) > 0:
                        return msgs[-1].get("content", "")
                return ""

            # 4. Transformer: String to Message Object
            if node["type"] == "hub/create_message":
                if node.get("inputs") and len(node["inputs"]) > 0 and node["inputs"][0].get("link"):
                    n, idx = find_source_node_and_slot(node["inputs"][0]["link"])
                    text = await resolve_node_value(n, idx)
                    role = node.get("properties", {}).get("role", "user")
                    return {"role": role, "content": str(text)}
                return None

            # 5. Transformer: Append to Messages
            if node["type"] == "hub/append_message":
                history =[]
                if node.get("inputs") and len(node["inputs"]) > 0 and node["inputs"][0].get("link"):
                    n, idx = find_source_node_and_slot(node["inputs"][0]["link"])
                    history = await resolve_node_value(n, idx)
                content = ""
                if node.get("inputs") and len(node["inputs"]) > 1 and node["inputs"][1].get("link"):
                    n, idx = find_source_node_and_slot(node["inputs"][1]["link"])
                    content = await resolve_node_value(n, idx)
                    
                role = node.get("properties", {}).get("role", "user")
                if not isinstance(history, list): history = [history] if history else[]
                updated = list(history)
                if content:
                    updated.append({"role": role, "content": str(content)})
                return updated
                
            # System Modifier
            if node["type"] == "hub/system_modifier":
                history =[]
                if node.get("inputs") and len(node["inputs"]) > 0 and node["inputs"][0].get("link"):
                    n, idx = find_source_node_and_slot(node["inputs"][0]["link"])
                    history = await resolve_node_value(n, idx)
                sys_prompt = ""
                if node.get("inputs") and len(node["inputs"]) > 1 and node["inputs"][1].get("link"):
                    n, idx = find_source_node_and_slot(node["inputs"][1]["link"])
                    sys_prompt = await resolve_node_value(n, idx)
                
                if not isinstance(history, list): history = [history] if history else []
                # Force override of system prompt
                updated = [m for m in history if m.get("role") != "system"]
                if sys_prompt:
                    updated.insert(0, {"role": "system", "content": str(sys_prompt)})
                return updated

            # 6. Entry Point
            if node["type"] == "hub/input":
                if output_slot_idx == 0: return messages
                elif output_slot_idx == 1: return {}

            # 7. Tool Definition (YAML Frontmatter)
            if node["type"] == "hub/tool":
                props = node.get("properties", {})
                metadata = props.get("metadata", {})
                return {
                    "type": "function",
                    "function": {
                        "name": metadata.get("name"),
                        "description": metadata.get("description", ""),
                        "parameters": metadata.get("parameters", {"type": "object", "properties": {}})
                    }
                }

            return None

            # 1.8 Logic: Enhanced Auto Router
            if node["type"] == "hub/autorouter":
                props = node.get("properties", {})
                
                # A. Resolve Input Context
                history = []
                if node.get("inputs") and node["inputs"][0].get("link"):
                    history = await resolve_node_value(*find_source_node_and_slot(node["inputs"][0]["link"]))
                
                user_text = (history[-1].get("content", "") if history else "").lower()
                
                # B. FAST RULES EVALUATION (Regex/Keywords)
                selected_slot = -1
                for rule in props.get("rules", []):
                    kw_str = rule.get("keywords", "").strip()
                    if not kw_str: continue
                    
                    keywords = [k.strip().lower() for k in kw_str.split(",")]
                    for kw in keywords:
                        # Regex support
                        if kw.startswith("/") and kw.endswith("/"):
                            try:
                                if re.search(kw[1:-1], user_text, re.I):
                                    selected_slot = rule["slot"]
                                    break
                            except: pass
                        # Literal support
                        elif kw in user_text:
                            selected_slot = rule["slot"]
                            break
                    if selected_slot != -1: break

                # C. SEMANTIC EVALUATION (Fast LLM Classifier)
                if selected_slot == -1 and props.get("use_semantic") and props.get("rules"):
                    classifier_model = props.get("classifier_model", "auto")
                    
                    # Construct panel of intents
                    intents_map = {r["slot"]: r["intent"] for r in props["rules"] if r["intent"]}
                    if intents_map:
                        intent_list = "\n".join([f"- PATH {s}: {d}" for s, d in intents_map.items()])
                        prompt = (
                            f"Classify this user message: '{user_text}'\n\n"
                            "Available Paths:\n" + intent_list + "\n\n"
                            "Output ONLY the Path identifier (e.g. 'PATH 2'). If none match, output 'PATH 1'."
                        )
                        
                        event_manager.emit(ProxyEvent("active", request_id, "Auto Router", classifier_model, sender, error_message="Classifying semantic intent..."))
                        
                        servers = await server_crud.get_servers_with_model(db, classifier_model)
                        if servers:
                            resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": classifier_model, "messages": [{"role": "user", "content": prompt}], "stream": False}).encode(), is_subrequest=True)
                            if hasattr(resp, 'body'):
                                ans = json.loads(resp.body.decode()).get("message", {}).get("content", "").upper()
                                match = re.search(r'PATH (\d+)', ans)
                                if match: selected_slot = int(match.group(1))

                # D. Fallback & Execution
                if selected_slot == -1: selected_slot = 1 # Default to first path
                
                # Check if slot is within current bounds
                if selected_slot >= len(node.get("inputs", [])): selected_slot = 1
                
                target_link = node["inputs"][selected_slot].get("link")
                if not target_link: return "Router picked empty slot."
                
                return await execute_cognitive_path(target_link, history)

            # 7. Tool Definition
            if node["type"] == "hub/tool":
                props = node.get("properties", {})
                metadata = props.get("metadata", {})
                if "type" in metadata:
                    return metadata
                if "name" in metadata and "description" in metadata:
                    return {
                        "type": "function",
                        "function": {
                            "name": metadata.get("name"),
                            "description": metadata.get("description"),
                            "parameters": metadata.get("parameters", {"type": "object", "properties": {}})
                        }
                    }
                return None

            return None

        # Trace back from Exit to find the primary cognitive block
        if "inputs" in exit_node and exit_node["inputs"][0].get("link") is not None:
            active_node, _ = find_source_node_and_slot(exit_node["inputs"][0]["link"])
            
            if active_node and active_node["type"] in ("hub/llm_chat", "hub/llm_instruct"):
                props = active_node.get("properties", {})
                target_model = str(props.get("model", "auto")).strip()
                final_temp = 0.7 # Default
                
                # Resolve Messages (Slot 0 for both Chat and Instruct)
                resolved_messages = messages
                if "inputs" in active_node and len(active_node["inputs"]) > 0 and active_node["inputs"][0].get("link") is not None:
                    n, idx = find_source_node_and_slot(active_node["inputs"][0]["link"])
                    res = await resolve_node_value(n, idx)
                    if res:
                        if isinstance(res, list):
                            resolved_messages = res
                        else:
                            # Instruct passes string in slot 0
                            resolved_messages = [{"role": "user", "content": str(res)}]

                # Resolve Settings
                if "inputs" in active_node and len(active_node["inputs"]) > 1 and active_node["inputs"][1].get("link") is not None:
                    n, idx = find_source_node_and_slot(active_node["inputs"][1]["link"])
                    settings = await resolve_node_value(n, idx)
                    if isinstance(settings, dict) and "temperature" in settings:
                        final_temp = float(settings["temperature"])

                # Resolve Model Override (Slot 2)
                model_override = None
                if "inputs" in active_node and len(active_node["inputs"]) > 2 and active_node["inputs"][2].get("link") is not None:
                    n, idx = find_source_node_and_slot(active_node["inputs"][2]["link"])
                    model_override = await resolve_node_value(n, idx)
                
                if model_override:
                    target_model = str(model_override).strip()

                # Resolve Tools (Slot 3 and beyond)
                final_tools =[]
                if "inputs" in active_node:
                    for i in range(3, len(active_node["inputs"])):
                        inp = active_node["inputs"][i]
                        if inp.get("link") is not None:
                            n, idx = find_source_node_and_slot(inp["link"])
                            tool_data = await resolve_node_value(n, idx)
                            if tool_data:
                                if isinstance(tool_data, list):
                                    final_tools.extend(tool_data)
                                else:
                                    final_tools.append(tool_data)

                if request:
                    request.state.graph_temperature = final_temp
                    if final_tools:
                        request.state.graph_tools = final_tools

                logger.info(f"Workflow '{name}' resolved: Node={active_node['type']}, Model={target_model}")
                return await _resolve_target(db, target_model, resolved_messages, depth + 1, request=request)

        return await _resolve_target(db, "auto", messages, depth + 1, request=request)

    # 1. Resolve Virtual Agent (Persona + RAG + MCP)
    agent_res = await db.execute(select(VirtualAgent).filter(VirtualAgent.name == name, VirtualAgent.is_active == True))
    agent = agent_res.scalars().first()
    if agent:
        logger.info(f"Hydrating Agent '{name}' -> Base: {agent.base_model}")

        import copy
        # Ensure deep copy of input messages
        updated_messages = copy.deepcopy(messages)

        # Soul Injection: Always ensure the agent system prompt is the first message
        # We strip existing system prompts to ensure the Agent's soul takes precedence
        updated_messages = [m for m in updated_messages if m.get("role") != "system"]
        updated_messages.insert(0, {"role": "system", "content": agent.system_prompt})

        return await _resolve_target(db, agent.base_model, updated_messages, depth + 1, request=request)

    # 2. Resolve Smart Router (formerly Pool)
    router_res = await db.execute(select(SmartRouter).filter(SmartRouter.name == name, SmartRouter.is_active == True))
    router = router_res.scalars().first()
    if router:
        # Mark router resolution for strict context enforcement
        if request is not None:
            request.state.enforce_strict_context = True
        # Fallback to the first target in the router for recursive resolution
        chosen_target = router.targets[0] if router.targets else name
        return await _resolve_target(db, chosen_target, messages, depth + 1, request=request)

    # 3. Fallback: It's a raw model name
    if name == "auto":
        body = {"messages": messages}
        best_model = await _select_auto_model(db, body)
        return best_model or "unknown", messages

    # For raw models, don't enforce strict context (preserve user's num_ctx if set)
    return name, messages

async def _call_classifier(request: Request, db: AsyncSession, classifier_model: str, last_message: str, intent_description: str, sender: str = "anon") -> bool:
    """Uses a small LLM to check if the current message matches a specific semantic intent."""
    classifier_prompt = (
        f"Analyze the following user message. Does it match the intent of '{intent_description}'?\n"
        f"Answer ONLY with 'YES' or 'NO'.\n\n"
        f"USER MESSAGE: {last_message}"
    )
    
    payload = {
        "model": classifier_model,
        "messages": [{"role": "user", "content": classifier_prompt}],
        "stream": False,
        "options": {"num_predict": 5, "temperature": 0}
    }
    
    try:
        servers = await server_crud.get_servers_with_model(db, classifier_model)
        if not servers: return False
        
        # Internal sub-request (non-streaming)
        resp, _ = await _reverse_proxy(
            request, "chat", servers, 
            json.dumps(payload).encode(), 
            sender=sender,
            is_subrequest=True
        )
        
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            answer = data.get("message", {}).get("content", "").strip().upper()
            return "YES" in answer
        return False
    except Exception as e:
        logger.error(f"Router Classifier Error: {e}")
        return False

async def _select_from_pool(db: AsyncSession, pool_name: str, body: Dict[str, Any], request: Request, sender: str = "anon") -> Optional[str]:
    """Selects a model using Multi-Tiered Hierarchical Rules (Fast + LLM)."""
    # Mark router calls for strict context enforcement
    request.state.enforce_strict_context = True
    # Mark as router call - context will be enforced when the resolved model is called
    request.state.enforce_strict_context = True
    from app.database.models import SmartRouter
    import copy
    
    res = await db.execute(select(SmartRouter).filter(SmartRouter.name == pool_name))
    pool = res.scalars().first()
    if not pool or not pool.targets:
        return None

    # CRITICAL FIX: Work on a deep copy to prevent polluting the original body
    body = copy.deepcopy(body)

    # 1. Extract context features (Fast)
    prompt_text = body.get("prompt") or ""
    has_images_in_msgs = False

    if "messages" in body:
        # Get LAST user message for intent classification
        for msg in reversed(body["messages"]):
            if not isinstance(msg, dict): continue
            
            # Detect images in OpenAI-style or Ollama-style messages
            if msg.get("images"):
                has_images_in_msgs = True
            
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict): continue
                    if part.get("type") == "image_url":
                        has_images_in_msgs = True
            elif not prompt_text:
                prompt_text = content

    features = {
        "has_images": bool(body.get("images") or has_images_in_msgs),
        "len": len(prompt_text),
        "sender": sender,
        "text": prompt_text.lower()
    }

    # 2. Hierarchical Rule Evaluation
    if pool.rules:
        for group in pool.rules:
            logic = group.get("logic", "OR")
            conditions = group.get("conditions", [])
            matches = []

            for cond in conditions:
                c_type = cond.get("type")
                c_val = str(cond.get("value", ""))
                c_match = False

                if c_type == "has_images": c_match = features["has_images"]
                elif c_type == "min_len": c_match = features["len"] >= int(c_val or 0)
                elif c_type == "max_len": c_match = features["len"] <= int(c_val or 0)
                elif c_type == "keyword": c_match = c_val.lower() in features["text"]
                elif c_type == "regex": 
                    try: c_match = bool(re.search(c_val, features["text"], re.I))
                    except: c_match = False
                elif c_type == "user": c_match = features["sender"] == c_val
                elif c_type == "intent" and pool.classifier_model:
                    c_match = await _call_classifier(request, db, pool.classifier_model, features["text"], c_val, sender=sender)

                matches.append(c_match)

            if matches and (all(matches) if logic == "AND" else any(matches)):
                logger.info(f"Router Match: Pool '{pool_name}' -> '{group['target']}'")
                return group['target']

    # 3. Fallback Strategy
    valid_targets = pool.targets
    
    if pool.strategy == 'random':
        return secrets.choice(valid_targets)
    
    if pool.strategy == 'least_loaded':
        # Strategy to maximize TPS: find server with fewest active connections
        # For simplicity, we prioritize models that are currently 'running' (cached in memory)
        active_models = [m['name'] for m in await server_crud.get_active_models_all_servers(db, httpx.AsyncClient())]
        running_in_pool = [m for m in valid_targets if m in active_models]
        if running_in_pool:
            return secrets.choice(running_in_pool)
        return valid_targets[0]

    # Default to priority (first in list)
    return valid_targets[0]

async def _handle_chain_request(db: AsyncSession, request: Request, chain_name: str, body: Dict[str, Any], api_key: APIKey, request_id: str):
    """Orchestrates sequential model execution (Swarm/Chain)."""
    from app.database.models import ChainOrchestrator
    
    # Mark chain calls for strict context enforcement
    request.state.enforce_strict_context = True
    
    result = await db.execute(select(ChainOrchestrator).filter(ChainOrchestrator.name == chain_name))
    chain = result.scalars().first()
    if not chain:
        raise HTTPException(status_code=404, detail="Chain not found")

    # Mark this as a chain call for strict context enforcement
    request.state.enforce_strict_context = True

    async def chain_generator():
        current_messages = body.get("messages", []).copy()
        final_content = ""

        for i, step_model in enumerate(chain.steps):
            is_last = (i == len(chain.steps) - 1)
            
            # Resolve the step model
            real_model, updated_messages = await _resolve_target(db, step_model, current_messages)
            
            sub_body = body.copy()
            sub_body["model"] = real_model
            sub_body["messages"] = updated_messages
            sub_body["stream"] = False # Intermediates must be synchronous
            
            try:
                servers = await server_crud.get_servers_with_model(db, real_model)
                if not servers:
                    yield (json.dumps({"error": f"Model {real_model} not found"}) + "\n").encode()
                    return

                resp, _ = await _reverse_proxy(
                    request, "chat", servers, 
                    json.dumps(sub_body).encode(), 
                    is_subrequest=True,
                    request_id=f"{request_id}_{i}",
                    model=step_model,
                    sender=api_key.user.username
                )
                
                data = json.loads(resp.body.decode())
                content = data.get("message", {}).get("content", "")
                
                # Append output to history for next step
                current_messages.append({"role": "assistant", "content": content})
                final_content = content
                
                if not is_last:
                    yield (json.dumps({"model": chain_name, "message": {"role": "assistant", "content": f"Step {i+1} completed...\n"}, "done": False}) + "\n").encode()
                
            except Exception as e:
                yield (json.dumps({"error": f"Chain error at step {i}: {str(e)}"}) + "\n").encode()
                return

        # Stream final result
        yield (json.dumps({"model": chain_name, "message": {"role": "assistant", "content": final_content}, "done": True}) + "\n").encode()

    return StreamingResponse(chain_generator(), media_type="application/x-ndjson")

async def _handle_bundle_request(db: AsyncSession, request: Request, bundle_name: str, body: Dict[str, Any], api_key: APIKey, request_id: str):
    """Orchestrates parallel model execution and synthesis (Ensemble)."""
    from app.database.models import EnsembleOrchestrator
    from app.database.session import AsyncSessionLocal
    
    # Mark ensemble calls for strict context enforcement across all participant models
    request.state.enforce_strict_context = True
    
    result = await db.execute(select(EnsembleOrchestrator).filter(EnsembleOrchestrator.name == bundle_name))
    bundle = result.scalars().first()
    if not bundle:
        event_manager.emit(ProxyEvent("error", request_id, bundle_name, "none", api_key.user.username, error_message="Bundle not found"))
        raise HTTPException(status_code=404, detail="Bundle not found")

    # Mark this as a bundle call for strict context enforcement
    request.state.enforce_strict_context = True

    # --- Vision Processing Helper ---
    async def extract_image_descriptions(vision_model: str, images: List, messages: List[Dict], prompt_text: str, http_client: httpx.AsyncClient) -> str:
        """
        Sends images to a vision model to get descriptions.
        Returns a formatted string with image descriptions to prepend to the user's message.
        """
        # Find the last user prompt text to give context to the vision model
        user_query = prompt_text
        if not user_query and messages:
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        user_query = content
                    elif isinstance(content, list):
                        user_query = " ".join([p.get("text", "") for p in content if p.get("type") == "text"])
                    break
        
        vision_prompt = (
            "You are a precise image analyzer. Analyze the provided images "
            "and describe their contents in detail. Pay special attention to elements relevant to this user query:\n"
            f"\"{user_query}\"\n\n"
            "Be thorough but concise. Format your response as:\n"
            "【Image 1 Description】: <detailed description>\n"
            "【Image 2 Description】: <detailed description>\n"
            "...\n\n"
            "If an image is unclear or unreadable, state that clearly."
        )
        
        # Build vision message with images
        # Placing instruction in 'user' role is safer for vision models like LLaVA
        vision_messages =[
            {
                "role": "user",
                "content": [
                    *[{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}} for img in images],
                    {"type": "text", "text": vision_prompt}
                ]
            }
        ]
        
        vision_payload = {
            "model": vision_model,
            "messages": vision_messages,
            "stream": False,
            "options": {"temperature": 0.3}  # Low temp for consistent descriptions
        }
        
        try:
            async with AsyncSessionLocal() as vision_db:
                servers = await server_crud.get_servers_with_model(vision_db, vision_model)
                if not servers:
                    return "\n\n⚠️ [Vision processor not available on any server]\n"
                
                target_server = servers[0]
                vision_url = f"{target_server.url.rstrip('/')}/api/chat"
                
                # Emit vision processing event
                vision_req_id = f"{request_id}_vision"
                event_manager.emit(ProxyEvent(
                    event_type="assigned",
                    request_id=vision_req_id,
                    model=vision_model,
                    server=target_server.name,
                    sender="orchestrator",
                    request_type="VISION"
                ))
                
                timeout = httpx.Timeout(read=120.0, write=60.0, connect=5.0)
                response = await http_client.post(
                    vision_url,
                    json=vision_payload,
                    timeout=timeout,
                    headers={"Content-Type": "application/json"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    descriptions = data.get("message", {}).get("content", "")
                    event_manager.emit(ProxyEvent(
                        event_type="completed",
                        request_id=vision_req_id,
                        model=vision_model,
                        server=target_server.name,
                        sender="orchestrator"
                    ))
                    return f"\n\n📷 **Image Analysis**:\n{descriptions}\n"
                else:
                    error_text = response.text[:200]
                    event_manager.emit(ProxyEvent(
                        event_type="error",
                        request_id=vision_req_id,
                        model=vision_model,
                        server=target_server.name,
                        sender="orchestrator",
                        error_message=f"Vision API error: {response.status_code}"
                    ))
                    return f"\n\n⚠️ [Failed to analyze images: {response.status_code}]\n"
        except Exception as e:
            logger.error(f"Vision processing failed: {e}")
            return f"\n\n⚠️ [Vision processing error: {str(e)[:100]}]\n"


    async def bundle_orchestrator_generator():
        # Detect if the client expects Ollama Chat format or Generation format
        is_chat_mode = "messages" in body
        http_client: httpx.AsyncClient = request.app.state.http_client
        
        def format_proxy_chunk(content: str, is_done: bool = False):
            """Helper to ensure chunks match the expected format of the calling client."""
            # Use 'Z' suffix to match Ollama's standard UTC format
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            chunk = {
                "model": bundle_name,
                "created_at": now_iso,
                "done": is_done
            }
            if is_chat_mode:
                chunk["message"] = {"role": "assistant", "content": content}
            else:
                chunk["response"] = content
            return (json.dumps(chunk) + "\n").encode()

        try:
            # Define the helper FIRST to avoid NameError
            async def get_sub_response(p_name):
                # Assign a unique ID for this specific agent to visualize it in the Live Flow
                sub_req_id = f"{request_id}_{p_name}"
                event_manager.emit(ProxyEvent(
                    event_type="received", 
                    request_id=sub_req_id, 
                    model=p_name, 
                    sender=api_key.user.username,
                    request_type="AGENT"
                ))

                # Use a fresh session for the generator logic to avoid closed-session errors
                async with AsyncSessionLocal() as local_db:
                    sub_messages = body.get("messages", [])
                    real_model, final_messages = await _resolve_target(local_db, p_name, sub_messages, request=request)
                    
                    sub_body = body.copy()
                    sub_body["model"] = real_model
                    sub_body["stream"] = False # Participants must NOT stream for parallel gather
                    
                    try:
                        servers = await server_crud.get_servers_with_model(local_db, real_model)
                        if not servers: 
                            event_manager.emit(ProxyEvent("error", sub_req_id, p_name, "none", "orchestrator", error_message="Model not found"))
                            return p_name, f"Error: Model {real_model} not found."
                        
                        path_suffix = "chat" if "messages" in sub_body else "generate"
                        
                        # Use is_subrequest=True to ensure we get a synchronous body back
                        resp, server_obj = await _reverse_proxy(
                            request, path_suffix, servers, 
                            json.dumps(sub_body).encode(), 
                            request_id=sub_req_id,
                            model=p_name,
                            sender=api_key.user.username,
                            is_subrequest=True
                        )

                        # Sub-requests don't use the streaming wrapper, so we emit status manually
                        s_name = server_obj.name if server_obj else "unknown"
                        event_manager.emit(ProxyEvent("active", sub_req_id, p_name, s_name, api_key.user.username))

                        if hasattr(resp, 'body'):
                            try:
                                data = json.loads(resp.body.decode())
                                content = data.get("message", {}).get("content", "") or data.get("response", "")
                                # Signal completion immediately so it goes to trash without lag
                                event_manager.emit(ProxyEvent("completed", sub_req_id, p_name, s_name, api_key.user.username, token_count=len(content)//4))
                                return p_name, content
                            except Exception as parse_err:
                                event_manager.emit(ProxyEvent("error", sub_req_id, p_name, s_name, api_key.user.username, error_message=f"JSON Parse Error: {str(parse_err)}"))
                                return p_name, f"Error parsing response from {p_name}: {parse_err}"

                        event_manager.emit(ProxyEvent("error", sub_req_id, p_name, s_name, api_key.user.username, error_message="Invalid Response Object"))
                        return p_name, "Error: Unexpected response type from agent."
                    except Exception as e:
                        # Ensure the dot in Live Flow turns red and goes to trash
                        event_manager.emit(ProxyEvent("error", sub_req_id, p_name, "none", api_key.user.username, error_message=str(e)))
                        return p_name, f"Error from {p_name}: {str(e)}"

            # 1. Master Decision: Should we activate the herd?
            user_query = body.get("prompt") or (body.get("messages", [{}])[-1].get("content") if body.get("messages") else "N/A")
            if isinstance(user_query, list):
                user_query = " ".join([p.get("text", "") for p in user_query if p.get("type") == "text"])

            force_ensemble = body.get("force_ensemble", False)
            use_ensemble = force_ensemble

            if not bundle.parallel_participants:
                use_ensemble = False
            elif not use_ensemble:
                classifier_prompt = (
                    f"User query: '{user_query}'\n\n"
                    "Analyze the complexity of this query. Determine if this task requires multiple AI experts to solve, or if it can be handled by a single model.\n"
                    "You MUST respond 'YES' if the task involves:\n"
                    " - Writing, debugging, or building code/games/software (ESPECIALLY 'build a snake game').\n"
                    " - Complex reasoning, multi-step planning, or brainstorming.\n"
                    " - Legal, medical, or highly technical multi-disciplinary analysis.\n\n"
                    "You may respond 'NO' only if the task is:\n"
                    " - A trivial greeting (e.g., 'Hello', 'Hi').\n"
                    " - A very simple factual query (e.g., 'What time is it?', 'What is 2+2?').\n\n"
                    "If you are even slightly unsure, answer 'YES'.\n"
                    "Respond ONLY with 'YES' or 'NO'."
                )

                # Internal non-streaming request to the Master Model for classification
                master_body = body.copy()
                master_body["model"] = bundle.master_model
                master_body["stream"] = False
                if "prompt" in master_body: master_body["prompt"] = classifier_prompt
                else: master_body["messages"] = [{"role": "user", "content": classifier_prompt}]

                async with AsyncSessionLocal() as local_db:
                    servers = await server_crud.get_servers_with_model(local_db, bundle.master_model)

                if servers:
                    resp, _ = await _reverse_proxy(
                        request, "chat" if "messages" in master_body else "generate", servers, 
                        json.dumps(master_body).encode(), is_subrequest=True,
                        request_id=f"{request_id}_cls", model=bundle.master_model, sender=api_key.user.username
                    )
                    if hasattr(resp, 'body'):
                        data = json.loads(resp.body.decode())
                        decision = (data.get("message", {}).get("content", "") or data.get("response", "")).strip().upper()
                        if "YES" in decision:
                            use_ensemble = True
                            logger.info(f"[Ensemble:{bundle_name}] Master decided: CHALLENGING. Activating ensemble.")
                        else:
                            logger.info(f"[Ensemble:{bundle_name}] Master decided: SIMPLE. Bypassing ensemble.")

            # 2. Conditional Orchestration
            if not use_ensemble:
                # Bypass ensemble and stream directly from Master
                logger.info(f"[Ensemble:{bundle_name}] Routing directly to Master Model: {bundle.master_model}")

                # CRITICAL: Update the model name in the payload to the physical master model
                body["model"] = bundle.master_model

                master_path = "chat" if "messages" in body else "generate"
                async with AsyncSessionLocal() as local_db:
                    servers = await server_crud.get_servers_with_model(local_db, bundle.master_model)

                if not servers:
                    yield format_proxy_chunk("⚠️ [Master model not available]", is_done=True)
                    return

                target_server = servers[0]
                master_payload = body

                # Retrieve master model metadata for clamping
                async with AsyncSessionLocal() as meta_db:
                    m_meta = await get_metadata_by_model_name(meta_db, bundle.master_model)
                    m_limit = m_meta.max_context if m_meta else 32768

                if target_server.server_type == 'ollama':
                    normalized_bytes = _normalize_payload_for_ollama(json.dumps(body).encode('utf-8'), max_context_limit=m_limit)
                    master_payload = json.loads(normalized_bytes)

                # Ensure we use server_crud._get_auth_headers directly
                try:
                    async with request.app.state.http_client.stream(
                        "POST", 
                        f"{target_server.url.rstrip('/')}/api/{master_path}", 
                        json=master_payload, 
                        headers=server_crud._get_auth_headers(target_server), 
                        timeout=600.0
                    ) as resp:
                        # CRITICAL: Check status before streaming
                        if resp.status_code != 200:
                            err_body = await resp.aread()
                            error_msg = f"Backend Error {resp.status_code}: {err_body.decode()[:50]}"
                            logger.error(f"[Ensemble:{bundle_name}] Direct master route failed: {error_msg}")
                            yield format_proxy_chunk(f"⚠️ **{error_msg}**", is_done=True)
                            return

                        # TRACK if we received anything
                        received_anything = False
                        async for line in resp.aiter_lines():
                            if not line: continue
                            received_anything = True
                            logger.debug(f"[Ensemble:{bundle_name}] Master stream chunk: {line[:100]}...")
                            yield (line + "\n").encode()

                        if not received_anything:
                            logger.error(f"[Ensemble:{bundle_name}] Backend returned empty stream for master model {bundle.master_model}")
                            yield format_proxy_chunk(f"⚠️ **[Error: Backend returned empty response from {bundle.master_model}]**", is_done=True)
                except Exception as e:
                    logger.error(f"[Ensemble:{bundle_name}] Direct master route failed: {e}")
                    yield format_proxy_chunk(f"⚠️ **[Error: {str(e)[:50]}]**", is_done=True)
                return

            # (If ensemble is activated, proceed with original execution flow)
            if bundle.send_status_update:
                status_msg = f"✨ Orchestrating ensemble via: {', '.join(bundle.parallel_participants)}... gathering perspectives.\n\n"
                yield format_proxy_chunk(status_msg)
            else:
                yield format_proxy_chunk("⏳ _Gathering experts..._")

            # 2. Parallel Participant Execution
            logger.info(f"[Ensemble:{bundle_name}] Request ID: {request_id} - Activating {len(bundle.parallel_participants)} agents: {bundle.parallel_participants}")

            event_manager.emit(ProxyEvent("assigned", request_id, bundle_name, "orchestrator", api_key.user.username))
            tasks = [asyncio.create_task(get_sub_response(m)) for m in bundle.parallel_participants]
            
            if bundle.show_monologue:
                yield format_proxy_chunk("\n\n")

            # Await all parallel agents (No explicit timeout, relying on global HTTP client)
            logger.info(f"[Ensemble:{bundle_name}] Awaiting parallel agent responses...")
            task_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            success_agents = []
            failed_agents = []
            results_dict = {}

            for i, res in enumerate(task_results):
                p_name = bundle.parallel_participants[i]
                if isinstance(res, Exception):
                    logger.error(f"[Ensemble:{bundle_name}] Agent '{p_name}' crashed: {res}")
                    failed_agents.append(p_name)
                    results_dict[p_name] = f"Error: Agent execution failed."
                elif isinstance(res, tuple) and res[1].startswith("Error:"):
                    logger.warning(f"[Ensemble:{bundle_name}] Agent '{p_name}' returned error: {res[1]}")
                    failed_agents.append(p_name)
                    results_dict[p_name] = res[1]
                else:
                    success_agents.append(p_name)
                    results_dict[p_name] = res[1]

            # Optional Status Report
            if bundle.report_success_failure:
                report = f"\n\n--- 📊 Ensemble Status ---\n✅ OK: {', '.join(success_agents) if success_agents else 'None'}\n❌ Fail: {', '.join(failed_agents) if failed_agents else 'None'}\n-------------------------\n\n"
                yield format_proxy_chunk(report)

            # Fix Monologue: Yield intermediate thoughts if enabled
            if bundle.show_monologue:
                logger.info(f"[Ensemble:{bundle_name}] Streaming intermediate thoughts (Monologue mode)...")
                for p_name in bundle.parallel_participants:
                    content = results_dict.get(p_name, "No data")
                    monologue_chunk = f"\n\n### AGENT: {p_name}\n{content}"
                    yield format_proxy_chunk(monologue_chunk)

            # Re-assemble results in original order for the Master Synthesis
            agent_outputs = "\n\n".join([f"### AGENT: {p}\n{results_dict.get(p, 'Error')}" for p in bundle.parallel_participants])

            # Resilience check: Proceed as long as at least ONE agent worked
            if not success_agents:
                err_msg = f"Critical Failure: All {len(bundle.parallel_participants)} agents failed. Summary: {', '.join(failed_agents)}"
                logger.error(f"[Ensemble:{bundle_name}] {err_msg}")
                event_manager.emit(ProxyEvent("error", request_id, bundle_name, "orchestrator", api_key.user.username, error_message=err_msg))
                yield format_proxy_chunk(f"\n\n⚠️ **{err_msg}**", is_done=True)
                return

            # 4. Master Synthesis
            user_query = body.get("prompt") or (body.get("messages", [{}])[-1].get("content") if body.get("messages") else "N/A")
            if isinstance(user_query, list): # Handle multi-modal prompts
                user_query = " ".join([p.get("text", "") for p in user_query if p.get("type") == "text"])

            synthesis_prompt = f"### PANEL INPUTS:\n{agent_outputs}\n\n### USER QUERY:\n{user_query}\n\n### MANDATE:\nReview inputs and provide a high-quality unified synthesis."
            
            master_body = body.copy()
            master_body["model"] = bundle.master_model
            # Honors original client request. If client wanted non-stream, we don't force stream.
            master_body["stream"] = body.get("stream", True)

            if "prompt" in master_body: 
                # Prepend/append to existing prompt to preserve conditioning
                master_body["prompt"] = f"{master_body['prompt']}\n\n{synthesis_prompt}"
            else: 
                # Append synthesis_prompt to existing messages to preserve context
                current_messages = body.get("messages", []).copy()
                current_messages.append({"role": "user", "content": synthesis_prompt})
                master_body["messages"] = current_messages

            # Determine endpoint
            master_path = "chat" if "messages" in master_body else "generate"
            
            # Fresh lookup for the master model server
            async with AsyncSessionLocal() as local_db:
                servers = await server_crud.get_servers_with_model(local_db, bundle.master_model)
                # Create log entry for the synthesis phase
                master_log_id = await _async_log_usage(
                    local_db, api_key.id, f"/api/{master_path}", 200, None, bundle.master_model
                )
            
            if not servers:
                err_msg = f"Master model '{bundle.master_model}' not found for synthesis."
                event_manager.emit(ProxyEvent("error", request_id, bundle_name, bundle.master_model, api_key.user.username, error_message=err_msg))
                yield (json.dumps({"model": bundle_name, "error": err_msg, "done": True}) + "\n").encode()
                return

            try:
                # Manually stream from backend for the Master synthesis 
                # to allow rewriting model names in chunks.
                target_server = servers[0]
                from app.crud.server_crud import _get_auth_headers
                headers = _get_auth_headers(target_server)
                
                # Apply normalization if necessary
                final_master_body = master_body
                if target_server.server_type == 'ollama':
                    normalized_bytes = _normalize_payload_for_ollama(json.dumps(master_body).encode('utf-8'))
                    final_master_body = json.loads(normalized_bytes)

                master_url = f"{target_server.url.rstrip('/')}/api/{master_path}"
                logger.info(f"[Ensemble:{bundle_name}] Synthesis requested via model '{bundle.master_model}' on server '{target_server.name}'")
                event_manager.emit(ProxyEvent("assigned", request_id, bundle_name, target_server.name, sender=api_key.user.username))

                # Increased timeout to 1 hour (3600s) for complex synthesis
                async with request.app.state.http_client.stream(
                    "POST", master_url, json=final_master_body, headers=headers, timeout=3600.0
                ) as backend_resp:
                    if backend_resp.status_code != 200:
                        err_text = await backend_resp.aread()
                        logger.error(f"[Ensemble:{bundle_name}] Master synthesis failed ({backend_resp.status_code}): {err_text.decode()}")
                        yield (json.dumps({"error": f"Master backend error: {err_text.decode()}"}) + "\n").encode()
                        return

                    # Signal that synthesis is active to trigger the visual spinner
                    event_manager.emit(ProxyEvent("active", request_id, bundle_name, target_server.name, sender=api_key.user.username))

                    async for line in backend_resp.aiter_lines():
                        if not line: continue
                        try:
                            # Rewrite model name to match the bundle requested by user
                            chunk_data = json.loads(line)
                            chunk_data["model"] = bundle_name
                            
                            # If this is the final chunk, send the completion event to move particle to trash
                            if chunk_data.get("done"):
                                t_count = chunk_data.get("eval_count", 0) + chunk_data.get("prompt_eval_count", 0)
                                event_manager.emit(ProxyEvent("completed", request_id, bundle_name, target_server.name, api_key.user.username, token_count=t_count))
                            
                            yield (json.dumps(chunk_data) + "\n").encode()
                        except json.JSONDecodeError:
                            yield (line + "\n").encode()

            except Exception as e:
                logger.error(f"Master synthesis failed: {e}")
                event_manager.emit(ProxyEvent("error", request_id, bundle_name, bundle.master_model, api_key.user.username, error_message=str(e)))
                yield (json.dumps({"error": f"Synthesis failed: {str(e)}"}) + "\n").encode()

        except Exception as global_err:
            logger.error(f"Ensemble orchestration failed: {global_err}", exc_info=True)
            event_manager.emit(ProxyEvent("error", request_id, bundle_name, "orchestrator", api_key.user.username, error_message=str(global_err)))
            yield (json.dumps({"error": "Orchestration failed", "details": str(global_err)}) + "\n").encode()

    # If the client doesn't want a stream, we must aggregate the entire ensemble response
    if not body.get("stream", True):
        full_text = ""
        is_chat_mode = "messages" in body
        final_data = {
            "model": bundle_name,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "done": True
        }
        if is_chat_mode:
            final_data["message"] = {"role": "assistant", "content": ""}
        else:
            final_data["response"] = ""
        
        async for chunk in bundle_orchestrator_generator():
            # The generator yields bytes ending in \n. Handle buffers with multiple objects.
            decoded_chunk = chunk.decode('utf-8')
            lines = [l.strip() for l in decoded_chunk.split('\n') if l.strip()]
            
            for line in lines:
                try:
                    data = json.loads(line)
                    # Accumulate text from either chat or generate format
                    if "message" in data:
                        full_text += data["message"].get("content", "")
                    elif "response" in data:
                        full_text += data.get("response", "")
                    
                    # Capture final metadata from any chunk (preferring the one with usage stats)
                    if data.get("done") or not final_data.get("total_duration"):
                        final_data.update({k: v for k, v in data.items() if k not in ("message", "response")})
                except json.JSONDecodeError:
                    continue
        
        # Inject the fully aggregated text into the single response object
        if is_chat_mode:
            final_data["message"]["content"] = full_text
        else:
            final_data["response"] = full_text
            
        return JSONResponse(content=final_data)

    # Otherwise return the stream for UI or streaming clients
    return StreamingResponse(bundle_orchestrator_generator(), media_type="application/x-ndjson")

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
    # Initialize context enforcement flag (bundles/orchestrators will set to True)
    request.state.enforce_strict_context = False
    
    req_id = secrets.token_hex(4)
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

    # 1. Handle 'auto' model routing FIRST            
    if model_name == "auto":
        chosen_model_name = await _select_auto_model(db, body)
        if not chosen_model_name:
            raise HTTPException(status_code=503, detail="Auto-routing failed.")
        model_name = chosen_model_name
        body["model"] = model_name

    # 2. Handle Smart Router (Pool) Resolution
    from app.database.models import SmartRouter
    pool_check = await db.execute(select(SmartRouter).filter(SmartRouter.name == model_name))
    router_obj = pool_check.scalars().first()
    if router_obj:
        model_name = await _select_from_pool(db, model_name, body, request, sender=api_key.user.username)
        if not model_name:
            raise HTTPException(status_code=503, detail="Router has no available targets.")
        body["model"] = model_name

    requested_model_name = model_name

    # 3. Handle Vision Augmenter Pipeline
    try:
        from app.database.models import VisionAugmenter
        aug_check = await db.execute(select(VisionAugmenter).filter(VisionAugmenter.name == model_name))
        augmenter = aug_check.scalars().first()
        if augmenter:
            logger.info(f"[VisionAugmenter] Pipeline triggered for '{model_name}'. Analyzing payload for images...")
            is_chat_mode = "messages" in body
            has_images = "images" in body and body["images"]
            
            if not has_images and is_chat_mode:
                for msg in body.get("messages",[]):
                    if msg.get("images"):
                        has_images = True
                        break
                    content = msg.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if part.get("type") == "image_url":
                                has_images = True
                                break

            if has_images:
                logger.info(f"[VisionAugmenter] Images detected. Offloading to VLM: '{augmenter.vision_model}'")
                event_manager.emit(ProxyEvent("assigned", req_id + "_v", augmenter.vision_model, "vision-augmenter", api_key.user.username, request_type="VISION"))
                
                extracted_images =[]
                if body.get("images"):
                    extracted_images.extend(body["images"])
                    del body["images"]
                
                user_query = body.get("prompt", "")
                if is_chat_mode:
                    for msg in body.get("messages", []):
                        if msg.get("images"):
                            extracted_images.extend(msg["images"])
                            del msg["images"]
                        
                        content = msg.get("content")
                        if isinstance(content, list):
                            text_parts =[]
                            for part in content:
                                if part.get("type") == "image_url":
                                    url = part.get("image_url", {}).get("url", "")
                                    if url.startswith("data:"):
                                        extracted_images.append(url.split(",", 1)[1])
                                    else:
                                        extracted_images.append(url)
                                elif part.get("type") == "text":
                                    text_parts.append(part.get("text", ""))
                            msg["content"] = " ".join(text_parts)
                            
                        if msg.get("role") == "user":
                            user_query = msg.get("content", "")

                # Get description
                image_descriptions = await _extract_image_descriptions(
                    request, db, augmenter.vision_model, extracted_images[:10], user_query, request.app.state.http_client, sender=api_key.user.username
                )
                
                # Replace the images with the descriptions
                if is_chat_mode:
                    for i in range(len(body["messages"]) - 1, -1, -1):
                        msg = body["messages"][i]
                        if msg.get("role") == "user":
                            msg["content"] = f"### CONTEXTUAL IMAGE ANALYSIS:\n{image_descriptions}\n\n### USER QUERY:\n{msg.get('content', '')}"
                            break
                else:
                    body["prompt"] = f"### CONTEXTUAL IMAGE ANALYSIS:\n{image_descriptions}\n\n### USER QUERY:\n{body.get('prompt', '')}"

                logger.info(f"[VisionAugmenter] VLM analysis complete. Routing cleaned prompt to: '{augmenter.text_model}'")
            
            # Continue pipeline with the text model
            model_name = augmenter.text_model
            body["model"] = model_name
    except Exception as e:
        logger.error(f"VisionAugmenter error: {e}", exc_info=True)


    # 3.5 Resolve Workflow / VirtualAgent / Graph Tools
    if model_name and isinstance(body, dict):
        is_chat_mode = "messages" in body
        input_msgs = body.get("messages",[]) if is_chat_mode else [{"role": "user", "content": body.get("prompt", "")}]
        
        resolved_model_name, resolved_messages = await _resolve_target(db, model_name, input_msgs, request=request)
        
        if resolved_model_name != model_name or resolved_messages != input_msgs:
            model_name = resolved_model_name
            body["model"] = model_name
            if is_chat_mode:
                body["messages"] = resolved_messages
            else:
                body["prompt"] = resolved_messages[-1]["content"] if resolved_messages else ""

        if hasattr(request.state, 'graph_tools') and request.state.graph_tools:
            if "tools" not in body or not body["tools"]:
                body["tools"] =[]
            for t in request.state.graph_tools:
                if t not in body["tools"]:
                    body["tools"].append(t)

    # 4. Handle Capabilities Gatekeeper (Tools & Thinking & Graph Settings)
    if model_name and isinstance(body, dict):
        # Apply temperature resolved from graph (if any)
        graph_temp = getattr(request.state, 'graph_temperature', None)
        if graph_temp is not None:
            if "options" not in body: body["options"] = {}
            body["options"]["temperature"] = graph_temp
        # Apply temperature resolved from graph (if any)
        graph_temp = getattr(request.state, 'graph_temperature', None)
        if graph_temp is not None:
            if "options" not in body: body["options"] = {}
            body["options"]["temperature"] = graph_temp
        # --- TOOL SANITIZATION ---
        # Only forward 'tools' if the list actually has items to prevent 500 errors
        if "tools" in body:
            if isinstance(body["tools"], list) and len(body["tools"]) > 0:
                logger.info(f"Forwarding {len(body['tools'])} tools to backend.")
            else:
                # Remove empty tool definitions that crash some cloud providers
                del body["tools"]
                if "tool_choice" in body:
                    del body["tool_choice"]

        # --- REFINED THINKING LOGIC ---
        # 1. Fetch exact UI configuration for this model
        meta = await model_metadata_crud.get_metadata_by_model_name(db, model_name)
        ui_allows_thinking = meta.supports_thinking if meta else False
        
        # 2. Only act if the client EXPLICITLY requested thinking
        if "think" in body:
            if ui_allows_thinking:
                # Normal path: Translation for special models like gpt-oss
                if "gpt-oss" in model_name.lower() and body.get("think") is True:
                    body["think"] = "medium"
            else:
                # Warning path: Client asked, but Hub UI is configured to block it
                warning_msg = (
                    f"⚠️ Client requested 'think: true' for {model_name}, but 'Think (CoT)' "
                    f"is DISABLED in Models Manager. Parameter stripped to prevent error."
                )
                logger.warning(warning_msg)
                
                # Notify the Admin via the Live Flow Telemetry
                if req_id:
                    event_manager.emit(ProxyEvent(
                        event_type="received", # Yellow pulse
                        request_id=req_id,
                        model=model_name,
                        sender=api_key.user.username,
                        error_message=f"NOTICE: 'think' parameter ignored. To enable reasoning, go to 'Intelligence Layer > Models Manager' and check 'Think (CoT)' for {model_name}."
                    ))
                
                # Strip the parameter to protect standard backends
                del body["think"]
        
        # 4. Re-encode the optimized body
        body_bytes = json.dumps(body).encode('utf-8')

    # 4. Telemetry

    # Determine Request Type
    req_type = path.upper()
    if "CHAT" in req_type: req_type = "CHAT"
    elif "GENERATE" in req_type: req_type = "GEN"
    elif "EMBED" in req_type: req_type = "EMBED"

    # Estimate input tokens
    p_tokens = len(str(body)) // 4

    # --- CRITICAL: Emit 'received' event BEFORE routing logic ---
    # This ensures particles appear for Ensembles and Routers immediately.
    event_manager.emit(ProxyEvent(
        event_type="received", 
        request_id=req_id, 
        model=requested_model_name or model_name or "unknown",
        sender=api_key.user.username,
        request_type=req_type,
        prompt_tokens=p_tokens
    ))

    # Detect Pool
    from app.database.models import SmartRouter
    pool_check = await db.execute(select(SmartRouter).filter(SmartRouter.name == model_name))
    if pool_check.scalars().first():
        resolved_model = await _select_from_pool(db, model_name, body, request, sender=api_key.user.username)
        # Pool already sets enforce_strict_context
        if resolved_model:
            model_name = resolved_model
            body["model"] = model_name
            body_bytes = json.dumps(body).encode('utf-8')
        else:
            event_manager.emit(ProxyEvent("error", req_id, model_name, "none", api_key.user.username, error_message="Pool empty"))
            raise HTTPException(status_code=503, detail=f"Model Pool '{model_name}' has no available models.")

    # Detect Bundle (Ensemble) or Chain (Swarm)
    from app.database.models import EnsembleOrchestrator, ChainOrchestrator
    
    bundle_check = await db.execute(select(EnsembleOrchestrator).filter(EnsembleOrchestrator.name == model_name))
    if bundle_check.scalars().first():
        return await _handle_bundle_request(db, request, model_name, body, api_key, req_id)

    chain_check = await db.execute(select(ChainOrchestrator).filter(ChainOrchestrator.name == model_name))
    if chain_check.scalars().first():
        return await _handle_chain_request(db, request, model_name, body, api_key, req_id)

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
        # --- SECURITY CHECK: Distinguish between 'Not Found' and 'Not Allowed' ---
        physically_exists = False
        for s in servers:
            if s.available_models:
                models_list = s.available_models
                if isinstance(models_list, str):
                    try: models_list = json.loads(models_list)
                    except: continue
                
                if any(m.get("name") == model_name or (s.server_type == 'ollama' and m.get("name", "").startswith(f"{model_name}:")) for m in models_list):
                    physically_exists = True
                    break
        
        if physically_exists:
            logger.warning(f"Access Denied: Model '{model_name}' exists on backends but is blocked by administrative whitelists.")
            event_manager.emit(ProxyEvent("error", req_id, model_name, "none", sender=api_key.user.username, error_message="Model Not Allowed"))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access to model '{model_name}' is restricted by the administrator's whitelist policy."
            )
        else:
            logger.error(f"proxy_ollama: No candidate servers available for model '{model_name}'")
            event_manager.emit(ProxyEvent("error", req_id, model_name, "none", sender=api_key.user.username, error_message="No servers found"))
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
    
    # Explicitly check if the client wants a stream
    client_wants_stream = body.get("stream", True) if isinstance(body, dict) else True

    # Proxy to one of the candidate servers
    response, chosen_server = await _reverse_proxy(
        request, path, candidate_servers, body_bytes,
        api_key_id=api_key.id, log_id=log_id,
        request_id=req_id, 
        model=model_name or "unknown",
        sender=api_key.user.username,
        client_wants_stream=client_wants_stream
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
