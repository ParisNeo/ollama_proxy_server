# app/api/v1/routes/playground_chat.py
import logging
import json
import time
from typing import Optional, Union

from fastapi import APIRouter, Depends, Request, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from app.database.session import get_db
from app.database.models import User
from app.crud import server_crud
from app.api.v1.dependencies import validate_csrf_token_header
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.core.test_prompts import PREBUILT_TEST_PROMPTS
from app.api.v1.routes.proxy import _select_auto_model


logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/playground", response_class=HTMLResponse, name="admin_playground")
async def admin_playground_ui(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    model: Optional[str] = Query(None)
):
    from app.api.v1.dependencies import get_csrf_token
    context = get_template_context(request)
    context["model_groups"] = await server_crud.get_all_models_grouped_by_server(db, filter_type='chat')
    context["selected_model"] = model
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/model_playground.html", context)


@router.post("/playground-stream", name="admin_playground_stream", dependencies=[Depends(validate_csrf_token_header)])
async def admin_playground_stream(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    try:
        data = await request.json()
        model_name = data.get("model")
        messages = data.get("messages")
        think_option = data.get("think_option") # Can be True, "low", "medium", "high"
        
        if not model_name or not messages:
            return JSONResponse({"error": "Model and messages are required."}, status_code=400)

        # --- NEW: Handle 'auto' model routing ---
        if model_name == "auto":
            resolved_model = await _select_auto_model(db, data)
            if not resolved_model:
                error_payload = {"error": "Auto-routing could not find a suitable model."}
                return Response(json.dumps(error_payload), media_type="application/x-ndjson", status_code=503)
            
            logger.info(f"Playground 'auto' model resolved to -> '{resolved_model}'")
            model_name = resolved_model
        # --- END NEW ---

        # Handle base64 images, converting them to the format Ollama expects
        for msg in messages:
            if isinstance(msg.get("content"), list):
                new_content = []
                images_list = []
                for item in msg["content"]:
                    if item.get("type") == "text":
                        new_content.append(item["text"])
                    elif item.get("type") == "image_url":
                        base64_str = item["image_url"]["url"].split(",")[-1]
                        images_list.append(base64_str)
                
                msg["content"] = " ".join(new_content)
                if images_list:
                    msg["images"] = images_list

        http_client: httpx.AsyncClient = request.app.state.http_client
        
        servers_with_model = await server_crud.get_servers_with_model(db, model_name)
        if not servers_with_model:
            active_servers = [s for s in await server_crud.get_servers(db) if s.is_active]
            if not active_servers:
                error_payload = {"error": "No active backend servers available."}
                return Response(json.dumps(error_payload), media_type="application/x-ndjson", status_code=503)
            target_server = active_servers[0]
        else:
            target_server = servers_with_model[0]
        
        if target_server.server_type == 'vllm':
            from app.core.vllm_translator import translate_ollama_to_vllm_chat, vllm_stream_to_ollama_stream
            
            chat_url = f"{target_server.url.rstrip('/')}/v1/chat/completions"
            
            ollama_payload = {
                "model": model_name,
                "messages": messages,
                "stream": True,
            }
            if think_option is True:
                ollama_payload["think"] = True
            
            payload = translate_ollama_to_vllm_chat(ollama_payload)
            
            from app.crud.server_crud import _get_auth_headers
            headers = _get_auth_headers(target_server)

            async def event_stream_vllm():
                try:
                    async with http_client.stream("POST", chat_url, json=payload, timeout=600.0, headers=headers) as response:
                        if response.status_code != 200:
                            error_body = await response.aread()
                            error_text = error_body.decode('utf-8')
                            logger.error(f"vLLM backend returned error {response.status_code}: {error_text}")
                            error_payload = {"error": f"vLLM server error: {error_text}"}
                            yield json.dumps(error_payload).encode('utf-8')
                            return
                        
                        async for chunk in vllm_stream_to_ollama_stream(response.aiter_text(), model_name):
                            yield chunk
                except Exception as e:
                    logger.error(f"Error streaming from vLLM backend: {e}", exc_info=True)
                    error_payload = {"error": "Failed to stream from backend server.", "details": str(e)}
                    yield json.dumps(error_payload).encode('utf-8')
            
            return StreamingResponse(event_stream_vllm(), media_type="application/x-ndjson")

        else: # Ollama server
            chat_url = f"{target_server.url.rstrip('/')}/api/chat"
            payload = {"model": model_name, "messages": messages, "stream": True}

            if think_option:
                model_name_lower = model_name.lower()
                supported_think_models = ["qwen", "gpt-oss", "deepseek"]

                if any(keyword in model_name_lower for keyword in supported_think_models):
                    payload["think"] = think_option
                else:
                    logger.warning(f"Frontend requested thinking for '{model_name}', but it's not in the known support list. Ignoring 'think' parameter.")

            from app.crud.server_crud import _get_auth_headers
            headers = _get_auth_headers(target_server)

            async def event_stream_ollama():
                final_chunk_from_ollama = None
                total_eval_text = ""
                start_time = time.monotonic()
                thinking_block_open = False
                try:
                    async with http_client.stream("POST", chat_url, json=payload, timeout=600.0, headers=headers) as response:
                        if response.status_code != 200:
                            error_body = await response.aread()
                            error_text = error_body.decode('utf-8')
                            logger.error(f"Ollama backend returned error {response.status_code}: {error_text}")
                            error_payload = {"error": f"Ollama server error: {error_text}"}
                            yield (json.dumps(error_payload) + '\n').encode('utf-8')
                            return

                        buffer = ""
                        async for chunk_str in response.aiter_text():
                            buffer += chunk_str
                            lines = buffer.split('\n')
                            buffer = lines.pop()

                            for line in lines:
                                if not line.strip(): continue
                                try:
                                    data = json.loads(line)
                                    message = data.get("message", {})
                                    
                                    # Handle thinking tokens
                                    if "thinking" in message and message["thinking"]:
                                        think_content = message["thinking"]
                                        if not thinking_block_open:
                                            thinking_block_open = True
                                            think_content = "<think>" + think_content
                                        
                                        # Modify chunk to look like a content chunk for the UI
                                        data["message"]["content"] = think_content
                                        del data["message"]["thinking"]
                                        total_eval_text += think_content
                                        yield (json.dumps(data) + '\n').encode('utf-8')
                                        continue

                                    # Handle content tokens
                                    if "content" in message and message["content"]:
                                        if thinking_block_open:
                                            thinking_block_open = False
                                            # Send closing tag as its own chunk first
                                            closing_chunk = data.copy()
                                            closing_chunk["message"] = {"role": "assistant", "content": "</think>"}
                                            if "done" in closing_chunk: del closing_chunk["done"]
                                            yield (json.dumps(closing_chunk) + '\n').encode('utf-8')
                                        
                                        total_eval_text += message["content"]
                                        # Yield original content chunk
                                        yield (line + '\n').encode('utf-8')
                                        continue
                                    
                                    if data.get("done"):
                                        final_chunk_from_ollama = data
                                    else:
                                        # Forward any other non-content/thinking chunks
                                        yield (line + '\n').encode('utf-8')

                                except json.JSONDecodeError:
                                    logger.warning(f"Could not parse Ollama stream chunk: {line}, forwarding as is.")
                                    yield (line + '\n').encode('utf-8')
                        
                        if buffer.strip():
                            try:
                                data = json.loads(buffer.strip())
                                if data.get("done"):
                                    final_chunk_from_ollama = data
                                else:
                                    yield (buffer.strip() + '\n').encode('utf-8')
                            except json.JSONDecodeError:
                                 yield (buffer.strip() + '\n').encode('utf-8')

                    if thinking_block_open:
                        closing_chunk = {"model": model_name, "created_at": "...", "message": {"role": "assistant", "content": "</think>"}, "done": False}
                        yield (json.dumps(closing_chunk) + '\n').encode('utf-8')

                    if final_chunk_from_ollama:
                        if "eval_count" not in final_chunk_from_ollama or "eval_duration" not in final_chunk_from_ollama:
                            logger.warning("Ollama response did not include stats, calculating manually.")
                            end_time = time.monotonic()
                            final_chunk_from_ollama["eval_count"] = len(total_eval_text) // 4
                            final_chunk_from_ollama["eval_duration"] = int((end_time - start_time) * 1_000_000_000)
                        
                        yield (json.dumps(final_chunk_from_ollama) + '\n').encode('utf-8')
                    else:
                        logger.error("No 'done' chunk received from Ollama stream.")

                except Exception as e:
                    logger.error(f"Error streaming from Ollama backend: {e}", exc_info=True)
                    error_payload = {"error": "Failed to stream from backend server.", "details": str(e)}
                    yield json.dumps(error_payload).encode('utf-8')
            
            return StreamingResponse(event_stream_ollama(), media_type="application/x-ndjson")

    except Exception as e:
        logger.error(f"Error in chat stream endpoint: {e}", exc_info=True)
        return JSONResponse({"error": "An internal error occurred."}, status_code=500)

@router.get("/playground/test-prompts", name="admin_get_test_prompts")
async def admin_get_test_prompts(admin_user: User = Depends(require_admin_user)):
    return JSONResponse(PREBUILT_TEST_PROMPTS)