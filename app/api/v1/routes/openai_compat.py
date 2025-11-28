"""
OpenAI-compatible endpoints at root level for clients like MSTY
These endpoints are accessible without the /api prefix
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
from app.api.v1.dependencies import get_valid_api_key, rate_limiter, ip_filter, get_settings
from app.database.models import APIKey, OllamaServer
from app.crud import log_crud, server_crud
from app.schema.settings import AppSettingsModel
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


def translate_openai_to_ollama_chat(openai_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Converts OpenAI format to Ollama format for chat requests"""
    ollama_payload = {
        "model": openai_payload.get("model"),
        "stream": openai_payload.get("stream", False),
    }
    
    messages = openai_payload.get("messages", [])
    ollama_messages = []
    
    for msg in messages:
        ollama_msg = {"role": msg.get("role")}
        
        content = msg.get("content")
        if isinstance(content, str):
            ollama_msg["content"] = content
        elif isinstance(content, list):
            text_parts = []
            images = []
            for part in content:
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    image_url = part.get("image_url", {}).get("url", "")
                    if image_url.startswith("data:image"):
                        base64_str = image_url.split(",")[-1]
                        images.append(base64_str)
            
            ollama_msg["content"] = " ".join(text_parts)
            if images:
                ollama_msg["images"] = images
        
        ollama_messages.append(ollama_msg)
    
    ollama_payload["messages"] = ollama_messages
    
    if "temperature" in openai_payload:
        ollama_payload["options"] = {"temperature": openai_payload["temperature"]}
    if "max_tokens" in openai_payload:
        if "options" not in ollama_payload:
            ollama_payload["options"] = {}
        ollama_payload["options"]["num_predict"] = openai_payload["max_tokens"]
    
    return ollama_payload


def translate_ollama_to_openai_chat(ollama_data: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """Converts Ollama format to OpenAI format for chat responses"""
    message_content = ollama_data.get("message", {}).get("content", "")
    
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": message_content
            },
            "finish_reason": "stop" if ollama_data.get("done") else None
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": ollama_data.get("eval_count", 0),
            "total_tokens": ollama_data.get("eval_count", 0)
        }
    }


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


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def openai_chat_completions(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db),
    settings: AppSettingsModel = Depends(get_settings),
):
    """OpenAI-compatible chat completions endpoint"""
    body_bytes = await request.body()
    try:
        openai_payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    model_name = openai_payload.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    
    # Handle "auto" model - use existing proxy logic
    if model_name == "auto":
        from app.api.v1.routes.proxy import _select_auto_model
        ollama_payload = translate_openai_to_ollama_chat(openai_payload)
        try:
            chosen_model = await _select_auto_model(db, ollama_payload)
            if not chosen_model:
                logger.error("Auto-routing returned None - no suitable model found")
                raise HTTPException(status_code=503, detail="Auto-routing could not find a suitable model")
            logger.info(f"OpenAI 'auto' model resolved to -> '{chosen_model}'")
            model_name = chosen_model
            openai_payload["model"] = chosen_model
        except Exception as e:
            logger.error(f"Error in auto model selection: {e}", exc_info=True)
            raise HTTPException(status_code=503, detail=f"Auto-routing failed: {str(e)}")
    
    # Get servers
    servers = await server_crud.get_servers(db)
    active_servers = [s for s in servers if s.is_active]
    
    # Find servers with this model
    servers_with_model = await server_crud.get_servers_with_model(db, model_name)
    candidate_servers = servers_with_model if servers_with_model else active_servers
    
    logger.info(f"OpenAI chat: model='{model_name}', found {len(servers_with_model)} servers with model, {len(active_servers)} total active servers, using {len(candidate_servers)} candidate servers")
    
    if not candidate_servers:
        logger.error(f"No candidate servers found for model '{model_name}'")
        raise HTTPException(status_code=503, detail=f"No servers available for model '{model_name}'")
    
    http_client: AsyncClient = request.app.state.http_client
    is_streaming = openai_payload.get("stream", False)
    
    # Route to first available server
    for server in candidate_servers:
        try:
            if server.server_type == "openrouter":
                server_api_key = decrypt_data(server.encrypted_api_key) if server.encrypted_api_key else None
                if not server_api_key:
                    continue
                
                headers = get_openrouter_headers(server_api_key)
                backend_url = f"{OPENROUTER_BASE_URL}/chat/completions"
                
                if is_streaming:
                    async def stream_gen():
                        async with http_client.stream("POST", backend_url, json=openai_payload, timeout=600.0, headers=headers) as response:
                            if response.status_code != 200:
                                error_body = await response.aread()
                                yield f"data: {json.dumps({'error': {'message': error_body.decode()}})}\n\n"
                                return
                            async for line in response.aiter_lines():
                                if line:
                                    yield f"{line}\n"
                    
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/chat/completions", status_code=200, server_id=server.id, model=model_name)
                    return StreamingResponse(stream_gen(), media_type="text/event-stream")
                else:
                    response = await http_client.post(backend_url, json=openai_payload, timeout=600.0, headers=headers)
                    response.raise_for_status()
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/chat/completions", status_code=response.status_code, server_id=server.id, model=model_name)
                    return JSONResponse(content=response.json())
            
            elif server.server_type == "vllm":
                headers = {}
                if server.encrypted_api_key:
                    api_key_dec = decrypt_data(server.encrypted_api_key)
                    if api_key_dec:
                        headers["Authorization"] = f"Bearer {api_key_dec}"
                
                backend_url = f"{server.url.rstrip('/')}/v1/chat/completions"
                
                if is_streaming:
                    async def stream_gen():
                        async with http_client.stream("POST", backend_url, json=openai_payload, timeout=600.0, headers=headers) as response:
                            if response.status_code != 200:
                                error_body = await response.aread()
                                yield f"data: {json.dumps({'error': {'message': error_body.decode()}})}\n\n"
                                return
                            async for line in response.aiter_lines():
                                if line:
                                    yield f"{line}\n"
                    
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/chat/completions", status_code=200, server_id=server.id, model=model_name)
                    return StreamingResponse(stream_gen(), media_type="text/event-stream")
                else:
                    response = await http_client.post(backend_url, json=openai_payload, timeout=600.0, headers=headers)
                    response.raise_for_status()
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/chat/completions", status_code=response.status_code, server_id=server.id, model=model_name)
                    return JSONResponse(content=response.json())
            
            else:  # Ollama
                ollama_payload = translate_openai_to_ollama_chat(openai_payload)
                ollama_body_bytes = json.dumps(ollama_payload).encode('utf-8')
                backend_url = f"{server.url.rstrip('/')}/api/chat"
                
                if is_streaming:
                    async def stream_gen():
                        buffer = ""
                        async with http_client.stream("POST", backend_url, content=ollama_body_bytes, timeout=600.0) as response:
                            if response.status_code != 200:
                                error_body = await response.aread()
                                yield f"data: {json.dumps({'error': {'message': error_body.decode()}})}\n\n"
                                return
                            
                            async for chunk_bytes in response.aiter_bytes():
                                buffer += chunk_bytes.decode('utf-8', errors='ignore')
                                lines = buffer.split('\n')
                                buffer = lines.pop() if lines else ""
                                
                                for line in lines:
                                    if not line.strip():
                                        continue
                                    try:
                                        ollama_data = json.loads(line)
                                        content = ollama_data.get("message", {}).get("content", "")
                                        if content:
                                            openai_chunk = {
                                                "id": f"chatcmpl-{int(time.time())}",
                                                "object": "chat.completion.chunk",
                                                "created": int(time.time()),
                                                "model": model_name,
                                                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
                                            }
                                            yield f"data: {json.dumps(openai_chunk)}\n\n"
                                        
                                        if ollama_data.get("done"):
                                            final_chunk = {
                                                "id": f"chatcmpl-{int(time.time())}",
                                                "object": "chat.completion.chunk",
                                                "created": int(time.time()),
                                                "model": model_name,
                                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                                            }
                                            yield f"data: {json.dumps(final_chunk)}\n\n"
                                            yield "data: [DONE]\n\n"
                                            return
                                    except (json.JSONDecodeError, KeyError):
                                        continue
                    
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/chat/completions", status_code=200, server_id=server.id, model=model_name)
                    return StreamingResponse(stream_gen(), media_type="text/event-stream")
                else:
                    response = await http_client.post(backend_url, content=ollama_body_bytes, timeout=600.0)
                    response.raise_for_status()
                    ollama_data = response.json()
                    openai_data = translate_ollama_to_openai_chat(ollama_data, model_name)
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/chat/completions", status_code=response.status_code, server_id=server.id, model=model_name)
                    return JSONResponse(content=openai_data)
        
        except Exception as e:
            logger.warning(f"Server '{server.name}' failed: {e}. Trying next server.")
            continue
    
    raise HTTPException(status_code=503, detail=f"All servers failed for model '{model_name}'")


def translate_openai_to_ollama_embeddings(openai_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Converts OpenAI embeddings format to Ollama format"""
    return {
        "model": openai_payload.get("model"),
        "prompt": openai_payload.get("input", ""),  # OpenAI uses "input", Ollama uses "prompt"
    }


def translate_ollama_to_openai_embeddings(ollama_data: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """Converts Ollama embeddings format to OpenAI format"""
    embedding = ollama_data.get("embedding", [])
    
    return {
        "object": "list",
        "data": [{
            "object": "embedding",
            "embedding": embedding,
            "index": 0
        }],
        "model": model_id,
        "usage": {
            "prompt_tokens": 0,
            "total_tokens": 0
        }
    }


@router.post("/v1/embeddings")
@router.post("/embeddings")
async def openai_embeddings(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db),
    settings: AppSettingsModel = Depends(get_settings),
):
    """OpenAI-compatible embeddings endpoint"""
    body_bytes = await request.body()
    try:
        openai_payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    model_name = openai_payload.get("model")
    input_text = openai_payload.get("input")
    
    if not model_name:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    if not input_text:
        raise HTTPException(status_code=400, detail="Missing 'input' field")
    
    # Get servers
    servers = await server_crud.get_servers(db)
    active_servers = [s for s in servers if s.is_active]
    
    # Find servers with this model
    servers_with_model = await server_crud.get_servers_with_model(db, model_name)
    candidate_servers = servers_with_model if servers_with_model else active_servers
    
    if not candidate_servers:
        raise HTTPException(status_code=503, detail=f"No servers available for model '{model_name}'")
    
    http_client: AsyncClient = request.app.state.http_client
    
    # Route to first available server
    for server in candidate_servers:
        try:
            if server.server_type == "openrouter":
                server_api_key = decrypt_data(server.encrypted_api_key) if server.encrypted_api_key else None
                if not server_api_key:
                    continue
                
                headers = get_openrouter_headers(server_api_key)
                backend_url = f"{OPENROUTER_BASE_URL}/embeddings"
                
                # OpenAI format is already compatible with OpenRouter
                response = await http_client.post(backend_url, json=openai_payload, timeout=60.0, headers=headers)
                response.raise_for_status()
                await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/embeddings", status_code=response.status_code, server_id=server.id, model=model_name)
                return JSONResponse(content=response.json())
            
            elif server.server_type == "vllm":
                headers = {}
                if server.encrypted_api_key:
                    api_key_dec = decrypt_data(server.encrypted_api_key)
                    if api_key_dec:
                        headers["Authorization"] = f"Bearer {api_key_dec}"
                
                backend_url = f"{server.url.rstrip('/')}/v1/embeddings"
                
                # OpenAI format is already compatible with vLLM
                response = await http_client.post(backend_url, json=openai_payload, timeout=60.0, headers=headers)
                response.raise_for_status()
                await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/embeddings", status_code=response.status_code, server_id=server.id, model=model_name)
                return JSONResponse(content=response.json())
            
            else:  # Ollama
                ollama_payload = translate_openai_to_ollama_embeddings(openai_payload)
                ollama_body_bytes = json.dumps(ollama_payload).encode('utf-8')
                backend_url = f"{server.url.rstrip('/')}/api/embeddings"
                
                response = await http_client.post(backend_url, content=ollama_body_bytes, timeout=60.0)
                response.raise_for_status()
                ollama_data = response.json()
                openai_data = translate_ollama_to_openai_embeddings(ollama_data, model_name)
                await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/embeddings", status_code=response.status_code, server_id=server.id, model=model_name)
                return JSONResponse(content=openai_data)
        
        except Exception as e:
            logger.warning(f"Server '{server.name}' failed: {e}. Trying next server.")
            continue
    
    raise HTTPException(status_code=503, detail=f"All servers failed for model '{model_name}'")


@router.post("/v1/completions")
@router.post("/completions")
async def openai_completions(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db),
    settings: AppSettingsModel = Depends(get_settings),
):
    """OpenAI-compatible legacy text completions endpoint (converts to chat/completions)"""
    body_bytes = await request.body()
    try:
        openai_payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    model_name = openai_payload.get("model")
    prompt = openai_payload.get("prompt")
    
    if not model_name:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    if not prompt:
        raise HTTPException(status_code=400, detail="Missing 'prompt' field")
    
    # Convert legacy completions format to chat/completions format
    # Legacy: {"model": "x", "prompt": "text"}
    # Chat: {"model": "x", "messages": [{"role": "user", "content": "text"}]}
    chat_payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "stream": openai_payload.get("stream", False),
    }
    
    # Copy over compatible parameters
    for param in ["temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty", "stop", "n"]:
        if param in openai_payload:
            chat_payload[param] = openai_payload[param]
    
    # Handle "auto" model - use existing proxy logic
    if model_name == "auto":
        from app.api.v1.routes.proxy import _select_auto_model
        ollama_payload = translate_openai_to_ollama_chat(chat_payload)
        try:
            chosen_model = await _select_auto_model(db, ollama_payload)
            if not chosen_model:
                raise HTTPException(status_code=503, detail="Auto-routing could not find a suitable model")
            model_name = chosen_model
            chat_payload["model"] = chosen_model
        except Exception as e:
            logger.error(f"Error in auto model selection: {e}", exc_info=True)
            raise HTTPException(status_code=503, detail=f"Auto-routing failed: {str(e)}")
    
    # Get servers
    servers = await server_crud.get_servers(db)
    active_servers = [s for s in servers if s.is_active]
    
    # Find servers with this model
    servers_with_model = await server_crud.get_servers_with_model(db, model_name)
    candidate_servers = servers_with_model if servers_with_model else active_servers
    
    if not candidate_servers:
        raise HTTPException(status_code=503, detail=f"No servers available for model '{model_name}'")
    
    http_client: AsyncClient = request.app.state.http_client
    is_streaming = chat_payload.get("stream", False)
    
    # Route to first available server (same logic as chat completions)
    for server in candidate_servers:
        try:
            if server.server_type == "openrouter":
                server_api_key = decrypt_data(server.encrypted_api_key) if server.encrypted_api_key else None
                if not server_api_key:
                    continue
                
                headers = get_openrouter_headers(server_api_key)
                backend_url = f"{OPENROUTER_BASE_URL}/chat/completions"
                
                if is_streaming:
                    async def stream_gen():
                        async with http_client.stream("POST", backend_url, json=chat_payload, timeout=600.0, headers=headers) as response:
                            if response.status_code != 200:
                                error_body = await response.aread()
                                yield f"data: {json.dumps({'error': {'message': error_body.decode()}})}\n\n"
                                return
                            async for line in response.aiter_lines():
                                if line:
                                    yield f"{line}\n"
                    
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/completions", status_code=200, server_id=server.id, model=model_name)
                    return StreamingResponse(stream_gen(), media_type="text/event-stream")
                else:
                    response = await http_client.post(backend_url, json=chat_payload, timeout=600.0, headers=headers)
                    response.raise_for_status()
                    chat_response = response.json()
                    # Convert chat response to legacy completions format
                    choices = chat_response.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                        completions_response = {
                            "id": chat_response.get("id", f"cmpl-{int(time.time())}"),
                            "object": "text_completion",
                            "created": chat_response.get("created", int(time.time())),
                            "model": model_name,
                            "choices": [{
                                "text": content,
                                "index": 0,
                                "logprobs": None,
                                "finish_reason": choices[0].get("finish_reason", "stop")
                            }],
                            "usage": chat_response.get("usage", {})
                        }
                        await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/completions", status_code=response.status_code, server_id=server.id, model=model_name)
                        return JSONResponse(content=completions_response)
            
            elif server.server_type == "vllm":
                headers = {}
                if server.encrypted_api_key:
                    api_key_dec = decrypt_data(server.encrypted_api_key)
                    if api_key_dec:
                        headers["Authorization"] = f"Bearer {api_key_dec}"
                
                backend_url = f"{server.url.rstrip('/')}/v1/chat/completions"
                
                if is_streaming:
                    async def stream_gen():
                        async with http_client.stream("POST", backend_url, json=chat_payload, timeout=600.0, headers=headers) as response:
                            if response.status_code != 200:
                                error_body = await response.aread()
                                yield f"data: {json.dumps({'error': {'message': error_body.decode()}})}\n\n"
                                return
                            async for line in response.aiter_lines():
                                if line:
                                    yield f"{line}\n"
                    
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/completions", status_code=200, server_id=server.id, model=model_name)
                    return StreamingResponse(stream_gen(), media_type="text/event-stream")
                else:
                    response = await http_client.post(backend_url, json=chat_payload, timeout=600.0, headers=headers)
                    response.raise_for_status()
                    chat_response = response.json()
                    # Convert chat response to legacy completions format
                    choices = chat_response.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                        completions_response = {
                            "id": chat_response.get("id", f"cmpl-{int(time.time())}"),
                            "object": "text_completion",
                            "created": chat_response.get("created", int(time.time())),
                            "model": model_name,
                            "choices": [{
                                "text": content,
                                "index": 0,
                                "logprobs": None,
                                "finish_reason": choices[0].get("finish_reason", "stop")
                            }],
                            "usage": chat_response.get("usage", {})
                        }
                        await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/completions", status_code=response.status_code, server_id=server.id, model=model_name)
                        return JSONResponse(content=completions_response)
            
            else:  # Ollama
                ollama_payload = translate_openai_to_ollama_chat(chat_payload)
                ollama_body_bytes = json.dumps(ollama_payload).encode('utf-8')
                backend_url = f"{server.url.rstrip('/')}/api/chat"
                
                if is_streaming:
                    async def stream_gen():
                        buffer = ""
                        async with http_client.stream("POST", backend_url, content=ollama_body_bytes, timeout=600.0) as response:
                            if response.status_code != 200:
                                error_body = await response.aread()
                                yield f"data: {json.dumps({'error': {'message': error_body.decode()}})}\n\n"
                                return
                            
                            async for chunk_bytes in response.aiter_bytes():
                                buffer += chunk_bytes.decode('utf-8', errors='ignore')
                                lines = buffer.split('\n')
                                
                                for line in lines[:-1]:  # Process all complete lines
                                    if not line.strip():
                                        continue
                                    try:
                                        ollama_data = json.loads(line)
                                        content = ollama_data.get("message", {}).get("content", "")
                                        if content:
                                            # Legacy completions format
                                            completions_chunk = {
                                                "id": f"cmpl-{int(time.time())}",
                                                "object": "text_completion",
                                                "created": int(time.time()),
                                                "model": model_name,
                                                "choices": [{"text": content, "index": 0, "logprobs": None, "finish_reason": None}]
                                            }
                                            yield f"data: {json.dumps(completions_chunk)}\n\n"
                                        
                                        if ollama_data.get("done"):
                                            final_chunk = {
                                                "id": f"cmpl-{int(time.time())}",
                                                "object": "text_completion",
                                                "created": int(time.time()),
                                                "model": model_name,
                                                "choices": [{"text": "", "index": 0, "logprobs": None, "finish_reason": "stop"}]
                                            }
                                            yield f"data: {json.dumps(final_chunk)}\n\n"
                                            yield "data: [DONE]\n\n"
                                            return
                                    except (json.JSONDecodeError, KeyError):
                                        continue
                                buffer = lines[-1]  # Keep the last (possibly incomplete) line
                    
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/completions", status_code=200, server_id=server.id, model=model_name)
                    return StreamingResponse(stream_gen(), media_type="text/event-stream")
                else:
                    response = await http_client.post(backend_url, content=ollama_body_bytes, timeout=600.0)
                    response.raise_for_status()
                    ollama_data = response.json()
                    message_content = ollama_data.get("message", {}).get("content", "")
                    completions_response = {
                        "id": f"cmpl-{int(time.time())}",
                        "object": "text_completion",
                        "created": int(time.time()),
                        "model": model_name,
                        "choices": [{
                            "text": message_content,
                            "index": 0,
                            "logprobs": None,
                            "finish_reason": "stop"
                        }],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": ollama_data.get("eval_count", 0),
                            "total_tokens": ollama_data.get("eval_count", 0)
                        }
                    }
                    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/completions", status_code=response.status_code, server_id=server.id, model=model_name)
                    return JSONResponse(content=completions_response)
        
        except Exception as e:
            logger.warning(f"Server '{server.name}' failed: {e}. Trying next server.")
            continue
    
    raise HTTPException(status_code=503, detail=f"All servers failed for model '{model_name}'")


@router.post("/v1/moderations")
@router.post("/moderations")
async def openai_moderations(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI-compatible moderations endpoint (returns safe content)"""
    body_bytes = await request.body()
    try:
        openai_payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    input_text = openai_payload.get("input")
    if not input_text:
        raise HTTPException(status_code=400, detail="Missing 'input' field")
    
    # For a proxy server, we'll return a safe moderation result
    # In a real implementation, you might want to route this to a moderation service
    input_list = input_text if isinstance(input_text, list) else [input_text]
    
    results = []
    for text in input_list:
        results.append({
            "flagged": False,
            "categories": {
                "hate": False,
                "hate/threatening": False,
                "harassment": False,
                "harassment/threatening": False,
                "self-harm": False,
                "self-harm/intent": False,
                "self-harm/instructions": False,
                "sexual": False,
                "sexual/minors": False,
                "violence": False,
                "violence/graphic": False
            },
            "category_scores": {
                "hate": 0.0,
                "hate/threatening": 0.0,
                "harassment": 0.0,
                "harassment/threatening": 0.0,
                "self-harm": 0.0,
                "self-harm/intent": 0.0,
                "self-harm/instructions": 0.0,
                "sexual": 0.0,
                "sexual/minors": 0.0,
                "violence": 0.0,
                "violence/graphic": 0.0
            }
        })
    
    await log_crud.create_usage_log(db=db, api_key_id=api_key.id, endpoint="/v1/moderations", status_code=200, server_id=None)
    return JSONResponse(content={
        "id": f"modr-{int(time.time())}",
        "model": "moderation-latest",
        "results": results
    })

