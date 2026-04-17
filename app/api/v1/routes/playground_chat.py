# app/api/v1/routes/playground_chat.py
import logging
import json
import time
import asyncio
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


async def _process_playground_logic(
    request: Request, db: AsyncSession, admin_user: User, data: dict, model_name: str, messages: list, think_option: any, req_id: str
):
    from app.database.models import SmartRouter, EnsembleOrchestrator, APIKey
    from sqlalchemy import select
    from app.core.events import event_manager, ProxyEvent
    from app.api.v1.routes.proxy import (
        _select_auto_model, 
        _select_from_pool, 
        _handle_bundle_request, 
        _resolve_target,
        _reverse_proxy,
        _async_log_usage
    )
    import secrets
    
    sender = admin_user.username

    # --- INITIAL TELEMETRY ---
    event_manager.emit(ProxyEvent(
        event_type="received", 
        request_id=req_id, 
        model=model_name,
        sender=sender,
        request_type="PLAYGROUND"
    ))

    # --- ORCHESTRATION LOGIC ---
        
    # 1. Handle 'auto' model
    if model_name == "auto":
        resolved_model = await _select_auto_model(db, data)
        if not resolved_model:
            event_manager.emit(ProxyEvent("error", req_id, model_name, "none", sender, error_message="Auto-routing failed"))
            return Response(json.dumps({"error": "Auto-routing could not find a suitable model."}), 
                            media_type="application/x-ndjson", status_code=503)
        model_name = resolved_model
        data["model"] = model_name

    # 2. Handle Smart Routers (Pools)
    pool_check = await db.execute(select(SmartRouter).filter(SmartRouter.name == model_name))
    if pool_check.scalars().first():
        resolved_model = await _select_from_pool(db, model_name, data, request, sender=sender)
        if resolved_model:
            model_name = resolved_model
            data["model"] = model_name
        else:
            event_manager.emit(ProxyEvent("error", req_id, model_name, "none", sender, error_message="Pool has no targets"))
            return Response(json.dumps({"error": f"Model Pool '{model_name}' has no targets."}), 
                            media_type="application/x-ndjson", status_code=503)

    # 3. Handle Ensemble Orchestrators (Bundles)
    bundle_check = await db.execute(select(EnsembleOrchestrator).filter(EnsembleOrchestrator.name == model_name))
    if bundle_check.scalars().first():
        # Create a detached APIKey object to satisfy orchestrator logging
        dummy_key = APIKey(user=admin_user, key_prefix="admin_playground")
        return await _handle_bundle_request(db, request, model_name, data, dummy_key, req_id)

    # 4. Resolve Virtual Agents (Recursively)
    # CRITICAL FIX: Deep copy messages before resolution to prevent mutation leakage
    import copy
    messages_copy = copy.deepcopy(messages)
    resolved_name, updated_messages = await _resolve_target(db, model_name, messages_copy, request=request, request_id=req_id, sender=sender)
    
    # --- STATIC RESULT INTERCEPTION ---
    # If the workflow returned a final text result (e.g. from a Composer node),
    # return it immediately instead of calling a backend model named "__result__"
    if resolved_name == "__result__":
        content = updated_messages[-1]["content"] if updated_messages else ""
        from datetime import datetime, timezone
        final_data = {
            "model": model_name,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "message": {"role": "assistant", "content": content},
            "done": True
        }
        # Emit completion event for the UI
        from app.core.events import event_manager, ProxyEvent
        event_manager.emit(ProxyEvent("completed", req_id, model_name, "Workflow Engine", sender, token_count=len(content)//4))
        return Response(json.dumps(final_data) + '\n', media_type="application/x-ndjson")

    model_name = resolved_name
    messages = updated_messages
    data["model"] = model_name
    data["messages"] = messages
    
    # Add tools if provided by workflow/agent
    if hasattr(request.state, 'graph_tools') and request.state.graph_tools:
        if "tools" not in data or not data["tools"]:
            data["tools"] =[]
        for t in request.state.graph_tools:
            if t not in data["tools"]:
                data["tools"].append(t)

    # --- END ORCHESTRATION ---

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
        error_payload = {"error": f"Model '{model_name}' is not available on any active backend server."}
        return Response(json.dumps(error_payload), media_type="application/x-ndjson", status_code=503)
        
    target_server = servers_with_model[0]
    
    if target_server.server_type in ('vllm', 'novita'):
        from app.core.vllm_translator import translate_ollama_to_vllm_chat, vllm_stream_to_ollama_stream
        
        chat_url = f"{target_server.url.rstrip('/')}/v1/chat/completions"
        
        ollama_payload = {
            "model": model_name,
            "messages": messages,
            "stream": True,
        }
        if think_option is not None:
            ollama_payload["think"] = think_option
        
        payload = translate_ollama_to_vllm_chat(ollama_payload)
        
        from app.crud.server_crud import _get_auth_headers
        headers = _get_auth_headers(target_server)

        async def event_stream_vllm():
            try:
                async with http_client.stream("POST", chat_url, json=payload, timeout=600.0, headers=headers) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        error_text = error_body.decode('utf-8')
                        srv_type = target_server.server_type.upper()
                        logger.error(f"{srv_type} backend returned error {response.status_code}: {error_text}")
                        error_payload = {"error": f"{srv_type} server error: {error_text}"}
                        yield json.dumps(error_payload).encode('utf-8')
                        return

                    async for chunk in vllm_stream_to_ollama_stream(response.aiter_text(), model_name):
                        yield chunk
            except (httpx.ReadError, asyncio.CancelledError):
                logger.warning("Stream interrupted (client disconnected or server shutdown).")
            except Exception as e:
                srv_type = target_server.server_type.upper()
                logger.error(f"Error streaming from {srv_type} backend: {e}", exc_info=True)
                error_payload = {"error": "Failed to stream from backend server.", "details": str(e)}
                yield (json.dumps(error_payload) + '\n').encode('utf-8')
        
        return StreamingResponse(event_stream_vllm(), media_type="application/x-ndjson")

    else: # Ollama server
        chat_url = f"{target_server.url.rstrip('/')}/api/chat"
        payload = {"model": model_name, "messages": messages, "stream": True}

        if think_option is not None:
            model_name_lower = model_name.lower()
            # Expand supported list to catch glm, qwq, etc.
            supported_think_models =["qwen", "gpt-oss", "deepseek", "glm", "qwq", "phi-4"]

            if any(keyword in model_name_lower for keyword in supported_think_models):
                payload["think"] = think_option
            elif think_option is True:
                logger.warning(f"Frontend requested thinking for '{model_name}', but it's not in the known support list. Ignoring 'think' parameter.")
            elif think_option is False:
                # Pass False anyway, as it might be a new reasoning model that defaults to True
                payload["think"] = False

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

            except (httpx.ReadError, asyncio.CancelledError):
                logger.warning("Stream interrupted (client disconnected or server shutdown).")
            except Exception as e:
                logger.error(f"Error streaming from Ollama backend: {e}", exc_info=True)
                error_payload = {"error": "Failed to stream from backend server.", "details": str(e)}
                yield (json.dumps(error_payload) + '\n').encode('utf-8')
        
        return StreamingResponse(event_stream_ollama(), media_type="application/x-ndjson")


@router.post("/playground-stream", name="admin_playground_stream", dependencies=[Depends(validate_csrf_token_header)])
async def admin_playground_stream(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    try:
        # STABILITY FIX: Initialize recursion tracking attributes for playground sessions
        request.state.processing_depth = 0
        request.state.enforce_strict_context = True
        request.state.source_platform = "Web Playground"
        data = await request.json()
        model_name = data.get("model")
        messages = data.get("messages")
        think_option = data.get("think_option")
        
        if not model_name or not messages:
            return JSONResponse({"error": "Model and messages are required."}, status_code=400)
            
        import secrets
        import asyncio
        from ascii_colors import trace_exception
        
        req_id = f"pg_{secrets.token_hex(4)}"
        
        stream_queue = asyncio.Queue()
        async def _stream_cb(text: str):
            await stream_queue.put(text)
        request.state.stream_callback = _stream_cb
        
        async def stream_generator():
            import datetime
            def format_chunk(content):
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                chunk = {
                    "model": model_name or "unknown",
                    "created_at": now_iso,
                    "done": False,
                    "message": {"role": "assistant", "content": content}
                }
                return (json.dumps(chunk) + "\n").encode()
                
            task = asyncio.create_task(_process_playground_logic(request, db, admin_user, data, model_name, messages, think_option, req_id))
            
            try:
                while not task.done():
                    try:
                        text = await asyncio.wait_for(stream_queue.get(), timeout=0.1)
                        yield format_chunk(text)
                    except asyncio.TimeoutError:
                        continue
                        
                try:
                    res = task.result()
                except Exception as e:
                    trace_exception(e)
                    yield format_chunk(f'<processing type="error" title="Playground Error">\n* {str(e)}\n</processing>\n')
                    yield (json.dumps({"model": model_name, "done": True}) + "\n").encode()
                    return
                    
                while not stream_queue.empty():
                    yield format_chunk(stream_queue.get_nowait())
                    
                if isinstance(res, StreamingResponse):
                    async for chunk in res.body_iterator:
                        yield chunk
                elif hasattr(res, 'body'):
                    yield res.body
            except asyncio.CancelledError:
                # If the client disconnects, cancel the background task to prevent DB session leaks
                task.cancel()
                raise
                
        return StreamingResponse(stream_generator(), media_type="application/x-ndjson")
        
    except Exception as e:
        from ascii_colors import trace_exception
        logger.error(f"Error in chat stream endpoint: {e}", exc_info=True)
        trace_exception(e)
        return JSONResponse({"error": "An internal error occurred."}, status_code=500)

@router.get("/playground/test-prompts", name="admin_get_test_prompts")
async def admin_get_test_prompts(admin_user: User = Depends(require_admin_user)):
    return JSONResponse(PREBUILT_TEST_PROMPTS)