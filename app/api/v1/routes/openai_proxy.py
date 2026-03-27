import json
import secrets
import logging
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.api.v1.dependencies import get_valid_api_key, rate_limiter, ip_filter
from app.database.models import APIKey

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(ip_filter), Depends(rate_limiter)])

@router.get("/models")
async def list_models(request: Request, db: AsyncSession = Depends(get_db), api_key: APIKey = Depends(get_valid_api_key)):
    if not request.app.state.settings.enable_openai_api:
        raise HTTPException(status_code=404, detail="OpenAI API is disabled in Hub settings.")
        
    from app.api.v1.routes.proxy import federate_models
    from app.crud.model_metadata_crud import get_all_metadata
    
    try:
        ollama_models = await federate_models(request, api_key, db)
        all_meta = await get_all_metadata(db)
        meta_map = {m.model_name: m for m in all_meta}
        
        data = []
        for m in ollama_models.get("models", []):
            m_name = m["name"]
            meta = meta_map.get(m_name)
            
            data.append({
                "id": m_name,
                "object": "model",
                "created": 1686935002,
                "owned_by": "lollms-hub",
                "context_window": meta.max_context if meta else 4096,
                "description": meta.description if meta else "Auto-discovered model."
            })
        return {"object": "list", "data": data}
    except Exception as e:
        logger.error(f"OpenAI Model List Error: {e}")
        return {"object": "list", "data": []}

@router.post("/chat/completions")
async def openai_chat(request: Request, db: AsyncSession = Depends(get_db), api_key: APIKey = Depends(get_valid_api_key)):
    if not request.app.state.settings.enable_openai_api:
        raise HTTPException(status_code=404, detail="OpenAI API is disabled in Hub settings.")
        
    from app.api.v1.routes.proxy import proxy_ollama, get_active_servers
    from app.api.v1.dependencies import get_settings
    
    body = await request.json()
    model_name = body.get("model")
    
    # OpenAI -> Ollama Format Translation
    hub_payload = {
        "model": model_name,
        "messages": body.get("messages"),
        "stream": body.get("stream", False),
        "tools": body.get("tools"),
        "tool_choice": body.get("tool_choice"),
        "options": {
            "temperature": body.get("temperature", 0.7),
            "top_p": body.get("top_p", 0.9),
            "num_predict": body.get("max_tokens"),
            "stop": body.get("stop")
        }
    }

    try:
        # Re-inject translated payload into main orchestration logic
        # We simulate the call to proxy_ollama
        response = await proxy_ollama(
            request=request,
            path="chat",
            api_key=api_key,
            db=db,
            settings=get_settings(request),
            servers=await get_active_servers(db)
        )

        # Response Translation: Ollama -> OpenAI
        if isinstance(response, StreamingResponse):
            async def openai_stream_wrapper():
                req_id = f"chatcmpl-{secrets.token_hex(12)}"
                async for chunk in response.body_iterator:
                    try:
                        line = chunk.decode('utf-8').strip()
                        if not line: continue
                        hub_data = json.loads(line)
                        
                        if hub_data.get("done"):
                            yield "data: [DONE]\n\n"
                            continue

                        delta = {}
                        msg = hub_data.get("message", {})
                        if "content" in msg: delta["content"] = msg["content"]
                        if "tool_calls" in msg: delta["tool_calls"] = msg["tool_calls"]
                        if "role" in msg: delta["role"] = msg["role"]

                        oa_chunk = {
                            "id": req_id, "object": "chat.completion.chunk", "created": 123456789,
                            "model": model_name,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(oa_chunk)}\n\n"
                    except: continue
            return StreamingResponse(openai_stream_wrapper(), media_type="text/event-stream")
        
        if hasattr(response, 'body'):
            hub_data = json.loads(response.body.decode())
            msg = hub_data.get("message", {})
            openai_msg = {"role": "assistant", "content": msg.get("content")}
            if "tool_calls" in msg: openai_msg["tool_calls"] = msg["tool_calls"]

            return {
                "id": f"chatcmpl-{secrets.token_hex(12)}", "object": "chat.completion", "created": 123456789,
                "model": model_name,
                "choices": [{
                    "index": 0, "message": openai_msg,
                    "finish_reason": "tool_calls" if "tool_calls" in openai_msg else "stop"
                }],
                "usage": {
                    "prompt_tokens": hub_data.get("prompt_eval_count", 0),
                    "completion_tokens": hub_data.get("eval_count", 0),
                    "total_tokens": hub_data.get("prompt_eval_count", 0) + hub_data.get("eval_count", 0)
                }
            }
        return response
    except Exception as e:
        logger.error(f"OpenAI Proxy Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))