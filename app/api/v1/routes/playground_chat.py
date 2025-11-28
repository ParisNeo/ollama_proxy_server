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
from app.crud import server_crud, conversation_crud
from app.api.v1.dependencies import validate_csrf_token_header
from app.api.v1.routes.admin import require_admin_user, get_template_context, templates
from app.core.test_prompts import PREBUILT_TEST_PROMPTS
from app.api.v1.routes.proxy import _select_auto_model
from app.schema.settings import AppSettingsModel

logger = logging.getLogger(__name__)
router = APIRouter()


async def _save_messages_to_conversation(
    db: AsyncSession,
    conversation_id: int,
    user_message: str,
    assistant_response: str,
    model_name: str,
    request: Request
):
    """Save user and assistant messages to conversation after streaming completes"""
    try:
        # Save user message
        if user_message:
            await conversation_crud.add_message(
                db, conversation_id, "user", user_message
            )
        
        # Save assistant message with embedding
        if assistant_response:
            embedding = None
            if hasattr(request.app.state, 'rag_service') and request.app.state.rag_service and request.app.state.rag_service.initialized:
                try:
                    embedding = request.app.state.rag_service.generate_embedding(assistant_response)
                except Exception as e:
                    logger.warning(f"Failed to generate embedding: {e}")
            
            await conversation_crud.add_message(
                db, conversation_id, "assistant", assistant_response,
                model_name=model_name, embedding=embedding
            )
            
            # If this is the first exchange, generate title
            messages = await conversation_crud.get_conversation_messages(db, conversation_id)
            if len(messages) == 2:  # User + Assistant
                first_exchange = await conversation_crud.get_first_exchange(db, conversation_id)
                if first_exchange:
                    # Generate title (simplified - use first 50 chars of user message)
                    title = user_message[:50] + "..." if len(user_message) > 50 else user_message
                    
                    # Generate and store embedding for first exchange
                    first_exchange_embedding = None
                    if hasattr(request.app.state, 'rag_service') and request.app.state.rag_service and request.app.state.rag_service.initialized:
                        try:
                            first_exchange_embedding = request.app.state.rag_service.generate_embedding(first_exchange)
                            await request.app.state.rag_service.add_conversation_embedding(
                                conversation_id, title, first_exchange
                            )
                        except Exception as e:
                            logger.warning(f"Failed to generate first exchange embedding: {e}")
                    
                    await conversation_crud.update_conversation(
                        db, conversation_id, title=title, first_exchange_embedding=first_exchange_embedding
                    )
    except Exception as e:
        logger.error(f"Error saving messages to conversation: {e}", exc_info=True)

@router.get("/playground", response_class=HTMLResponse, name="admin_playground")
async def admin_playground_ui(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    model: Optional[str] = Query(None)
):
    from app.api.v1.dependencies import get_csrf_token
    context = get_template_context(request)
    model_groups = await server_crud.get_all_models_grouped_by_server(db, filter_type='chat')
    
    # Add pricing information to model groups
    model_pricing = {}
    try:
        from app.core.pricing_utils import get_pricing_summary
        servers = await server_crud.get_servers(db)
        active_servers = [s for s in servers if s.is_active]
        
        # Build model details map
        model_to_details = {}
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
                        if model_name not in model_to_details:
                            model_to_details[model_name] = model_data.get("details", {})
        
        # Add pricing to model groups
        for model_name, details in model_to_details.items():
            if details.get("pricing"):
                try:
                    model_pricing[model_name] = get_pricing_summary(details["pricing"])
                except Exception as e:
                    logger.warning(f"Failed to get pricing summary for {model_name}: {e}")
                    continue
    except Exception as e:
        logger.error(f"Failed to build model pricing: {e}", exc_info=True)
        model_pricing = {}  # Ensure it's always a dict
    
    context["model_groups"] = model_groups
    context["model_pricing"] = model_pricing  # Always a dict, never Undefined
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
        verbosity = data.get("verbosity", "maximum")  # "short", "medium", "maximum"
        conversation_id = data.get("conversation_id")  # Optional: existing conversation ID
        
        if not model_name or not messages:
            return JSONResponse({"error": "Model and messages are required."}, status_code=400)
        
        # Get or create conversation
        from app.crud import conversation_crud
        if conversation_id:
            conversation = await conversation_crud.get_conversation(db, conversation_id, admin_user.id)
            if not conversation:
                return JSONResponse({"error": "Conversation not found."}, status_code=404)
            
            # Load conversation history from DB for context awareness
            # This ensures the model has full context even if frontend messages were edited
            conversation_messages = await conversation_crud.get_conversation_messages(db, conversation_id)
            if conversation_messages:
                # Convert DB messages to API format
                history_messages = []
                for msg in conversation_messages:
                    history_messages.append({
                        "role": msg.role,
                        "content": msg.content
                    })
                
                # The frontend sends all messages including history, but we use DB history
                # to ensure consistency. The last message in the frontend array is the new one.
                # Replace messages with DB history + new message from frontend
                new_message = messages[-1] if messages else None
                if new_message and new_message.get("role") == "user":
                    # Use DB history + new user message
                    messages = history_messages + [new_message]
                    logger.info(f"Loaded {len(history_messages)} previous messages + 1 new message for conversation {conversation_id}")
                else:
                    # No new message, use all DB history
                    messages = history_messages
                    logger.info(f"Loaded {len(history_messages)} messages from conversation {conversation_id}")
        else:
            # Create new conversation
            conversation = await conversation_crud.create_conversation(db, admin_user.id)
            conversation_id = conversation.id

        # --- NEW: Handle 'auto' model routing ---
        if model_name == "auto":
            resolved_model = await _select_auto_model(db, data)
            if not resolved_model:
                error_payload = {"error": "Auto-routing could not find a suitable model."}
                return Response(json.dumps(error_payload), media_type="application/x-ndjson", status_code=503)
            
            logger.info(f"Playground 'auto' model resolved to -> '{resolved_model}'")
            model_name = resolved_model
            data["_is_auto_model"] = True  # Flag for fallback retry
        # --- END NEW ---

        # Apply verbosity control to messages
        from app.core.verbosity_control import apply_verbosity_to_messages, apply_verbosity_to_params
        messages = apply_verbosity_to_messages(messages, verbosity)
        
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

        # --- WEB SEARCH INTEGRATION: Add web search context if needed ---
        # Check if web search is enabled in the request (default to True for backward compatibility)
        enable_web_search = data.get("enable_web_search", True)
        
        app_settings: AppSettingsModel = request.app.state.settings
        has_api_key = bool(app_settings.ollama_api_key or app_settings.ollama_api_key_2)
        logger.debug(f"Web search check: enable_web_search={enable_web_search}, has_api_key={has_api_key}, key1_set={bool(app_settings.ollama_api_key)}, key2_set={bool(app_settings.ollama_api_key_2)}")
        
        if enable_web_search and has_api_key:
            from app.core.unified_search import UnifiedSearchService
            from app.core.chat_web_search import needs_web_search, extract_search_query
            
            # Get the last user message
            last_user_message = None
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    last_user_message = msg
                    break
            
            if last_user_message and isinstance(last_user_message.get("content"), str):
                user_message = last_user_message["content"]
                
                if needs_web_search(user_message):
                    logger.info(f"Web search triggered for message: {user_message[:100]}")
                    try:
                        search_service = UnifiedSearchService(
                            searxng_url=app_settings.searxng_url if app_settings.searxng_url else "http://localhost:7019",
                            ollama_api_key=app_settings.ollama_api_key,
                            ollama_api_key_2=app_settings.ollama_api_key_2,
                            timeout=20.0
                        )
                        
                        search_query = extract_search_query(user_message)
                        logger.info(f"Performing web search for query: {search_query}")
                        
                        search_results = await search_service.web_search(
                            query=search_query,
                            max_results=5,
                            engine=None  # Auto: try SearXNG first, fallback to Ollama
                        )
                        
                        if search_results.get("results"):
                            logger.info(f"Web search returned {len(search_results['results'])} results")
                            # Format results as natural, flowing prose
                            from app.core.chat_web_search import format_search_results_naturally
                            web_context = format_search_results_naturally(search_results["results"], search_query)
                            
                            if web_context:
                                # Add web search context to the user's message directly
                                # This ensures it works with all server types (OpenRouter, vLLM, Ollama)
                                # Some models may not respect system messages, so we prepend to user message
                                original_content = last_user_message["content"]
                                enhanced_content = f"Current information from the web:\n\n{web_context}\n\nBased on this information, please answer: {original_content}"
                                last_user_message["content"] = enhanced_content
                                logger.info(f"✓ Enhanced user message with web search context (original: {len(original_content)}, enhanced: {len(enhanced_content)})")
                            else:
                                logger.warning("Web search returned results but formatting produced empty context")
                        else:
                            logger.warning(f"Web search returned no results for query: {search_query}")
                    except Exception as e:
                        logger.error(f"Error adding web search context: {e}", exc_info=True)
                        # Continue without web search if it fails
                else:
                    logger.debug(f"Web search not needed for message: {user_message[:100]}")
        else:
            logger.debug("Ollama API keys not configured, skipping web search")
        # --- END WEB SEARCH INTEGRATION ---

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
        
        if target_server.server_type == 'openrouter':
            from app.core.openrouter_translator import (
                translate_ollama_to_openrouter_chat,
                openrouter_stream_to_ollama_stream,
                get_openrouter_headers,
                OPENROUTER_BASE_URL
            )
            from app.core.encryption import decrypt_data
            
            # Get API key
            server_api_key = None
            if target_server.encrypted_api_key:
                server_api_key = decrypt_data(target_server.encrypted_api_key)
            
            if not server_api_key:
                error_payload = {"error": "OpenRouter requires an API key. Please configure one in server settings."}
                return Response(json.dumps(error_payload), media_type="application/x-ndjson", status_code=400)
            
            chat_url = f"{OPENROUTER_BASE_URL}/chat/completions"
            headers = get_openrouter_headers(server_api_key)
            
            ollama_payload = {
                "model": model_name,
                "messages": messages,
                "stream": True,
            }
            
            if think_option:
                ollama_payload["think"] = think_option
            
            # Apply verbosity parameters
            apply_verbosity_to_params(ollama_payload, verbosity)
            
            openrouter_payload = translate_ollama_to_openrouter_chat(ollama_payload)
            
            async def openrouter_stream():
                full_response = ""
                user_message = ""
                streamed_model_name = model_name
                current_model = model_name
                failed_models = []
                is_auto = data.get("_is_auto_model", False)
                
                while True:
                    try:
                        async with http_client.stream("POST", chat_url, json=openrouter_payload, timeout=600.0, headers=headers) as response:
                            if response.status_code != 200:
                                error_body = await response.aread()
                                error_text = error_body.decode()
                                error_lower = error_text.lower()
                                
                                # Check if it's a model-specific error (for auto-routing fallback)
                                is_model_error = (
                                    is_auto and
                                    response.status_code in (404, 400) and 
                                    ("model" in error_lower or "not found" in error_lower or "no such" in error_lower or "endpoint" in error_lower)
                                )
                                
                                if is_model_error:
                                    # Model failed, try next best model
                                    failed_models.append(current_model)
                                    logger.warning(f"Auto-routing: Model '{current_model}' failed ({response.status_code}): {error_text}. Trying next best model...")
                                    
                                    # Get next best model
                                    next_model = await _select_auto_model(db, data, skip_models=failed_models)
                                    if next_model:
                                        current_model = next_model
                                        ollama_payload["model"] = next_model
                                        openrouter_payload = translate_ollama_to_openrouter_chat(ollama_payload)
                                        logger.info(f"Auto-routing fallback: Selected '{next_model}'")
                                        continue  # Retry with next model
                                    else:
                                        # No more models available
                                        error_payload = {"error": f"Auto-routing: All models failed. Tried: {', '.join(failed_models)}"}
                                        yield (json.dumps(error_payload) + '\n').encode('utf-8')
                                        return
                                else:
                                    # Not a model error, yield error and return
                                    error_payload = {"error": f"OpenRouter error: {error_text}"}
                                    yield (json.dumps(error_payload) + '\n').encode('utf-8')
                                    return
                            
                            # Get last user message for saving
                            for msg in reversed(messages):
                                if msg.get("role") == "user":
                                    user_message = msg.get("content", "")
                                    break
                            
                            async for chunk in openrouter_stream_to_ollama_stream(response.aiter_text(), current_model):
                                # Extract content and model name from chunks
                                try:
                                    chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk
                                    for line in chunk_str.strip().split('\n'):
                                        if line:
                                            chunk_data = json.loads(line)
                                            if chunk_data.get("message", {}).get("content"):
                                                full_response += chunk_data["message"]["content"]
                                            if chunk_data.get("model"):
                                                streamed_model_name = chunk_data["model"]
                                except:
                                    pass
                                yield chunk
                        
                        # Success! Save messages and return
                        if len(failed_models) > 0:
                            logger.info(f"Auto-routing fallback: Successfully used '{current_model}' after {len(failed_models)} failed model(s)")
                        await _save_messages_to_conversation(
                            db, conversation_id, user_message, full_response, streamed_model_name, request
                        )
                        return  # Success, exit the retry loop
                    
                    except httpx.HTTPStatusError as e:
                        # HTTP error - check if model-specific (for auto-routing fallback)
                        error_text = ""
                        try:
                            error_text = e.response.text.lower()
                        except:
                            pass
                        
                        is_model_error = (
                            is_auto and
                            e.response.status_code in (404, 400) and 
                            ("model" in error_text or "not found" in error_text or "no such" in error_text or "endpoint" in error_text)
                        )
                        
                        if is_model_error:
                            # Model failed, try next best model
                            failed_models.append(current_model)
                            logger.warning(f"Auto-routing: Model '{current_model}' failed ({e.response.status_code}): {e.response.text}. Trying next best model...")
                            
                            # Get next best model
                            next_model = await _select_auto_model(db, data, skip_models=failed_models)
                            if next_model:
                                current_model = next_model
                                ollama_payload["model"] = next_model
                                openrouter_payload = translate_ollama_to_openrouter_chat(ollama_payload)
                                logger.info(f"Auto-routing fallback: Selected '{next_model}'")
                                continue  # Retry with next model
                            else:
                                # No more models available
                                logger.error(f"Auto-routing: All models failed. Tried: {', '.join(failed_models)}")
                                error_payload = {"error": f"Auto-routing: All models failed. Tried: {', '.join(failed_models)}"}
                                yield (json.dumps(error_payload) + '\n').encode('utf-8')
                                return
                        else:
                            # Not a model error
                            logger.error(f"OpenRouter HTTP error for '{current_model}': {e}")
                            error_payload = {"error": f"OpenRouter error ({e.response.status_code}): {e.response.text}"}
                            yield (json.dumps(error_payload) + '\n').encode('utf-8')
                            return
                    
                    except Exception as e:
                        # Non-HTTP error - if auto-routing, try next model
                        if is_auto:
                            failed_models.append(current_model)
                            logger.warning(f"Auto-routing: Model '{current_model}' failed with error: {e}. Trying next best model...")
                            
                            # Get next best model
                            next_model = await _select_auto_model(db, data, skip_models=failed_models)
                            if next_model:
                                current_model = next_model
                                ollama_payload["model"] = next_model
                                openrouter_payload = translate_ollama_to_openrouter_chat(ollama_payload)
                                logger.info(f"Auto-routing fallback: Selected '{next_model}'")
                                continue  # Retry with next model
                            else:
                                # No more models available
                                logger.error(f"Auto-routing: All models failed. Tried: {', '.join(failed_models)}")
                                error_payload = {"error": f"Auto-routing: All models failed. Tried: {', '.join(failed_models)}"}
                                yield (json.dumps(error_payload) + '\n').encode('utf-8')
                                return
                        else:
                            # Not auto-routing, just fail
                            logger.error(f"OpenRouter stream error for '{current_model}': {e}")
                            error_payload = {"error": f"OpenRouter stream error: {str(e)}"}
                            yield (json.dumps(error_payload) + '\n').encode('utf-8')
                            return
            
            response = StreamingResponse(openrouter_stream(), media_type="application/x-ndjson")
            response.headers["X-Conversation-Id"] = str(conversation_id)
            return response
        
        elif target_server.server_type == 'vllm':
            from app.core.vllm_translator import translate_ollama_to_vllm_chat, vllm_stream_to_ollama_stream
            
            chat_url = f"{target_server.url.rstrip('/')}/v1/chat/completions"
            
            ollama_payload = {
                "model": model_name,
                "messages": messages,
                "stream": True,
            }
            if think_option is True:
                ollama_payload["think"] = True
            
            # Apply verbosity parameters
            apply_verbosity_to_params(ollama_payload, verbosity)
            
            payload = translate_ollama_to_vllm_chat(ollama_payload)
            
            from app.crud.server_crud import _get_auth_headers
            headers = _get_auth_headers(target_server)

            async def event_stream_vllm():
                full_response = ""
                user_message = ""
                streamed_model_name = model_name
                try:
                    # Get last user message for saving
                    for msg in reversed(messages):
                        if msg.get("role") == "user":
                            user_message = msg.get("content", "")
                            break
                    
                    async with http_client.stream("POST", chat_url, json=payload, timeout=600.0, headers=headers) as response:
                        if response.status_code != 200:
                            error_body = await response.aread()
                            error_text = error_body.decode('utf-8')
                            logger.error(f"vLLM backend returned error {response.status_code}: {error_text}")
                            error_payload = {"error": f"vLLM server error: {error_text}"}
                            yield json.dumps(error_payload).encode('utf-8')
                            return
                        
                        async for chunk in vllm_stream_to_ollama_stream(response.aiter_text(), model_name):
                            # Extract content and model name from chunks
                            try:
                                chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk
                                for line in chunk_str.strip().split('\n'):
                                    if line:
                                        chunk_data = json.loads(line)
                                        if chunk_data.get("message", {}).get("content"):
                                            full_response += chunk_data["message"]["content"]
                                        if chunk_data.get("model"):
                                            streamed_model_name = chunk_data["model"]
                            except:
                                pass
                            yield chunk
                    
                    # Save messages to conversation after stream completes
                    await _save_messages_to_conversation(
                        db, conversation_id, user_message, full_response, streamed_model_name, request
                    )
                except Exception as e:
                    logger.error(f"Error streaming from vLLM backend: {e}", exc_info=True)
                    error_payload = {"error": "Failed to stream from backend server.", "details": str(e)}
                    yield json.dumps(error_payload).encode('utf-8')
            
            response = StreamingResponse(event_stream_vllm(), media_type="application/x-ndjson")
            response.headers["X-Conversation-Id"] = str(conversation_id)
            return response

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
            
            # Apply verbosity parameters
            apply_verbosity_to_params(payload, verbosity)

            from app.crud.server_crud import _get_auth_headers
            headers = _get_auth_headers(target_server)

            async def event_stream_ollama():
                final_chunk_from_ollama = None
                total_eval_text = ""
                start_time = time.monotonic()
                thinking_block_open = False
                user_message = ""
                streamed_model_name = model_name
                
                # Get last user message for saving
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        user_message = msg.get("content", "")
                        break
                
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
                    
                    # Save messages to conversation after stream completes
                    # Extract actual content (excluding thinking tokens)
                    actual_content = total_eval_text.replace("<think>", "").replace("</think>", "")
                    
                    await _save_messages_to_conversation(
                        db, conversation_id, user_message, actual_content, streamed_model_name, request
                    )
                except Exception as e:
                    logger.error(f"Error streaming from Ollama backend: {e}", exc_info=True)
                    error_payload = {"error": "Failed to stream from backend server.", "details": str(e)}
                    yield json.dumps(error_payload).encode('utf-8')
            
            response = StreamingResponse(event_stream_ollama(), media_type="application/x-ndjson")
            response.headers["X-Conversation-Id"] = str(conversation_id)
            return response

    except Exception as e:
        logger.error(f"Error in chat stream endpoint: {e}", exc_info=True)
        return JSONResponse({"error": "An internal error occurred."}, status_code=500)

@router.get("/playground/test-prompts", name="admin_get_test_prompts")
async def admin_get_test_prompts(admin_user: User = Depends(require_admin_user)):
    return JSONResponse(PREBUILT_TEST_PROMPTS)