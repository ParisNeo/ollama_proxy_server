# app/api/v1/routes/playground_chat.py
"""Chat playground routes for Ollama Proxy Server."""

import json
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_csrf_token, validate_csrf_token_header
from app.api.v1.routes.admin import get_template_context, require_admin_user, templates
from app.api.v1.routes.proxy import _select_auto_model
from app.core.test_prompts import PREBUILT_TEST_PROMPTS
from app.core.vllm_translator import translate_ollama_to_vllm_chat, vllm_stream_to_ollama_stream
from app.crud import server_crud
from app.crud.server_crud import _get_auth_headers
from app.database.models import User
from app.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/playground", response_class=HTMLResponse, name="admin_playground")
async def admin_playground_ui(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    _admin_user: User = Depends(require_admin_user),  # noqa: B008
    model: Optional[str] = Query(None),  # noqa: B008
):
    """Render admin playground UI."""

    context = get_template_context(request)
    context["model_groups"] = await server_crud.get_all_models_grouped_by_server(db, filter_type="chat")
    context["selected_model"] = model
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/model_playground.html", context)


def _vllm_playground_stream(
    http_client: httpx.AsyncClient,
    target_server,
    model_name: str,
    messages: list,
    think_option: bool,
) -> StreamingResponse:
    """Stream from vLLM playground with translation."""

    chat_url = f"{target_server.url.rstrip('/')}/v1/chat/completions"

    ollama_payload = {
        "model": model_name,
        "messages": messages,
        "stream": True,
    }
    if think_option is True:
        ollama_payload["think"] = True

    payload = translate_ollama_to_vllm_chat(ollama_payload)
    headers = _get_auth_headers(target_server)

    async def event_stream_vllm():
        try:
            async with http_client.stream("POST", chat_url, json=payload, timeout=600.0, headers=headers) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    error_text = error_body.decode("utf-8")
                    logger.error("vLLM backend returned error %s: %s", response.status_code, error_text)
                    error_payload = {"error": f"vLLM server error: {error_text}"}
                    yield json.dumps(error_payload).encode("utf-8")
                    return

                async for chunk in vllm_stream_to_ollama_stream(response.aiter_text(), model_name):
                    yield chunk
        except Exception as e:  # pylint: disable=broad-exception-caught)
            logger.error("Error streaming from vLLM backend: %s", str(e), exc_info=True)
            error_payload = {"error": "Failed to stream from backend server.", "details": str(e)}
            yield json.dumps(error_payload).encode("utf-8")

    return StreamingResponse(event_stream_vllm(), media_type="application/x-ndjson")


def _ollama_playground_stream(  # pylint: disable=too-many-statements
    http_client: httpx.AsyncClient,
    target_server,
    model_name: str,
    messages: list,
    think_option: bool,
) -> StreamingResponse:
    """Stream from ollama playground with translation."""
    # TODO: refactor to lower complexity
    chat_url = f"{target_server.url.rstrip('/')}/api/chat"

    ollama_payload = {
        "model": model_name,
        "messages": messages,
        "stream": True,
    }

    if think_option:
        ollama_payload["think"] = True
    else:
        logger.warning("Frontend requested thinking for '%s', but it's not in the known support list. Ignoring 'think' parameter.", model_name)

    headers = _get_auth_headers(target_server)

    async def event_stream_ollama():  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        """Handle event stream from ollama."""
        # TDOD: refactor to multiple functions to complex
        final_chunk_from_ollama = None
        total_eval_text = ""
        start_time = time.monotonic()
        thinking_block_open = False
        try:
            async with http_client.stream("POST", chat_url, json=ollama_payload, timeout=600.0, headers=headers) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    error_text = error_body.decode("utf-8")
                    logger.error("Ollama backend returned error %s: %s", response.status_code, error_text)
                    error_payload = {"error": f"Ollama server error: {error_text}"}
                    yield (json.dumps(error_payload) + "\n").encode("utf-8")
                    return

                buffer = ""
                async for chunk_str in response.aiter_text():
                    buffer += chunk_str
                    lines = buffer.split("\n")
                    buffer = lines.pop()

                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            message = data.get("message", {})

                            # Handle thinking tokens
                            if "thinking" in message and message["thinking"]:
                                think_content = message["thinking"]
                                if not thinking_block_open:
                                    thinking_block_open = True
                                else:
                                    think_content += " " + think_content
                                data["message"]["content"] = think_content
                                del data["message"]["thinking"]
                                total_eval_text += think_content
                                yield (json.dumps(data) + "\n").encode("utf-8")
                                continue

                            # Handle content tokens
                            if "content" in message and message["content"]:
                                if thinking_block_open:
                                    thinking_block_open = False
                                    # Send closing tag as its own chunk first
                                    closing_chunk = data.copy()
                                    closing_chunk["message"] = {"role": "assistant", "content": "</think>"}
                                    if "done" in closing_chunk:
                                        del closing_chunk["done"]
                                    yield (json.dumps(closing_chunk) + "\n").encode("utf-8")

                                total_eval_text += message["content"]
                                # Yield original content chunk
                                yield (line + "\n").encode("utf-8")
                                continue

                            if data.get("done"):
                                final_chunk_from_ollama = data
                            else:
                                # Forward any other non-content/thinking chunks
                                yield (line + "\n").encode("utf-8")

                        except json.JSONDecodeError:
                            logger.warning("Could not parse Ollama stream chunk: %s, forwarding as is.", line)
                            yield (line + "\n").encode("utf-8")

                if buffer.strip():
                    try:
                        data = json.loads(buffer.strip())
                        if data.get("done"):
                            final_chunk_from_ollama = data
                        else:
                            yield (buffer.strip() + "\n").encode("utf-8")
                    except json.JSONDecodeError:
                        yield (buffer.strip() + "\n").encode("utf-8")

            if thinking_block_open:
                closing_chunk = {"model": model_name, "created_at": "...", "message": {"role": "assistant", "content": "</think>"}, "done": False}
                yield (json.dumps(closing_chunk) + "\n").encode("utf-8")

            if final_chunk_from_ollama:
                if "eval_count" not in final_chunk_from_ollama or "eval_duration" not in final_chunk_from_ollama:
                    logger.warning("Ollama response did not include stats, calculating manually.")
                    end_time = time.monotonic()
                    final_chunk_from_ollama["eval_count"] = len(total_eval_text) // 4
                    final_chunk_from_ollama["eval_duration"] = int((end_time - start_time) * 1_000_000_000)

                yield (json.dumps(final_chunk_from_ollama) + "\n").encode("utf-8")
            else:
                logger.error("No 'done' chunk received from Ollama stream.")

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Error streaming from Ollama backend: %s", e, exc_info=True)
            error_payload = {"error": "Failed to stream from backend server.", "details": str(e)}
            yield json.dumps(error_payload).encode("utf-8")

    return StreamingResponse(event_stream_ollama(), media_type="application/x-ndjson")


def _convert_images(messages):
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


@router.post("/playground-stream", name="admin_playground_stream", dependencies=[Depends(validate_csrf_token_header)])  # noqa: B008
async def admin_playground_stream(request: Request, db: AsyncSession = Depends(get_db), _admin_user: User = Depends(require_admin_user)):  # noqa: B008
    """Handle playground streaming requests."""
    try:
        data = await request.json()
        model_name = data.get("model")
        messages = data.get("messages")
        think_option = data.get("think_option")  # Can be True, "low", "medium", "high"

        if not model_name or not messages:
            return JSONResponse({"error": "Model and messages are required."}, status_code=400)

        # --- NEW: Handle 'auto' model routing ---
        if model_name == "auto":
            resolved_model = await _select_auto_model(db, data)
            if not resolved_model:
                error_payload = {"error": "Auto-routing could not find a suitable model."}
                return Response(json.dumps(error_payload), media_type="application/x-ndjson", status_code=503)

            logger.info("Playground 'auto' model resolved to -> '%s'", resolved_model)
            model_name = resolved_model
        # --- END NEW ---

        # Handle base64 images, converting them to the format Ollama expects
        _convert_images(messages)

        http_client: httpx.AsyncClient = request.app.state.http_client

        servers_with_model = await server_crud.get_servers_with_model(db, model_name)
        if not servers_with_model:
            active_servers = [s for s in await server_crud.get_servers(db) if bool(s.is_active)]
            if not active_servers:
                error_payload = {"error": "No active backend servers available."}
                return Response(json.dumps(error_payload), media_type="application/x-ndjson", status_code=503)
            target_server = active_servers[0]

        target_server = servers_with_model[0]

        if target_server.server_type == "vllm":
            return _vllm_playground_stream(http_client, target_server, model_name, messages, think_option)

        return _ollama_playground_stream(http_client, target_server, model_name, messages, think_option)

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Error in chat stream endpoint: %s", e, exc_info=True)
        return JSONResponse({"error": "An internal error occurred."}, status_code=500)


@router.get("/playground/test-prompts", name="admin_get_test_prompts")
async def admin_get_test_prompts(_admin_user: User = Depends(require_admin_user)):  # noqa: B008
    """Get test prompts for playground."""
    return JSONResponse(PREBUILT_TEST_PROMPTS)
