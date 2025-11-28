"""
OpenRouter API Translator

Translates Ollama API requests to OpenRouter API format.
OpenRouter uses OpenAI-compatible API, so this is similar to vLLM but with
OpenRouter-specific features like auto-routing, model routing, provider routing, etc.
"""
import json
import logging
from typing import Dict, Any, AsyncGenerator, Optional
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# OpenRouter base URL - always the same
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# OpenRouter-specific parameters that should be passed through
OPENROUTER_SPECIFIC_PARAMS = {
    "transforms",  # Prompt transforms
    "models",      # Model routing (fallback list)
    "route",       # Route strategy (e.g., "fallback")
    "provider",    # Provider preferences
    "user",        # User identifier for abuse prevention
}


def translate_ollama_to_openrouter_chat(ollama_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Translates an Ollama chat request to OpenRouter format.
    Passes through all OpenRouter-specific parameters.
    """
    openrouter_payload = {
        "model": ollama_payload.get("model"),  # Can be None for auto-routing
        "stream": ollama_payload.get("stream", False),
    }
    
    # Copy messages directly (same format)
    messages = ollama_payload.get("messages", [])
    openrouter_payload["messages"] = messages
    
    # Translate image format if present (same as vLLM)
    for message in openrouter_payload["messages"]:
        if "images" in message and isinstance(message["images"], list):
            if message.get("content") and isinstance(message["content"], str):
                new_content = [{"type": "text", "text": message["content"]}]
                for img_b64 in message["images"]:
                    new_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                    })
                message["content"] = new_content
            del message["images"]
    
    # Copy standard OpenAI parameters
    if "temperature" in ollama_payload:
        openrouter_payload["temperature"] = ollama_payload["temperature"]
    if "max_tokens" in ollama_payload:
        openrouter_payload["max_tokens"] = ollama_payload["max_tokens"]
    if "top_p" in ollama_payload:
        openrouter_payload["top_p"] = ollama_payload["top_p"]
    if "top_k" in ollama_payload:
        openrouter_payload["top_k"] = ollama_payload["top_k"]
    if "frequency_penalty" in ollama_payload:
        openrouter_payload["frequency_penalty"] = ollama_payload["frequency_penalty"]
    if "presence_penalty" in ollama_payload:
        openrouter_payload["presence_penalty"] = ollama_payload["presence_penalty"]
    if "stop" in ollama_payload:
        openrouter_payload["stop"] = ollama_payload["stop"]
    if "seed" in ollama_payload:
        openrouter_payload["seed"] = ollama_payload["seed"]
    if "response_format" in ollama_payload:
        openrouter_payload["response_format"] = ollama_payload["response_format"]
    if "tools" in ollama_payload:
        openrouter_payload["tools"] = ollama_payload["tools"]
    if "tool_choice" in ollama_payload:
        openrouter_payload["tool_choice"] = ollama_payload["tool_choice"]
    if "logit_bias" in ollama_payload:
        openrouter_payload["logit_bias"] = ollama_payload["logit_bias"]
    if "top_logprobs" in ollama_payload:
        openrouter_payload["top_logprobs"] = ollama_payload["top_logprobs"]
    if "min_p" in ollama_payload:
        openrouter_payload["min_p"] = ollama_payload["min_p"]
    if "top_a" in ollama_payload:
        openrouter_payload["top_a"] = ollama_payload["top_a"]
    if "repetition_penalty" in ollama_payload:
        openrouter_payload["repetition_penalty"] = ollama_payload["repetition_penalty"]
    if "prediction" in ollama_payload:
        openrouter_payload["prediction"] = ollama_payload["prediction"]
    
    # Pass through OpenRouter-specific parameters
    for param in OPENROUTER_SPECIFIC_PARAMS:
        if param in ollama_payload:
            openrouter_payload[param] = ollama_payload[param]
    
    return openrouter_payload


def translate_ollama_to_openrouter_embeddings(ollama_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Translates an Ollama embeddings request to OpenRouter format.
    """
    openrouter_payload = {
        "model": ollama_payload.get("model"),
        "input": ollama_payload.get("prompt") or ollama_payload.get("input"),
    }
    
    # Pass through OpenRouter-specific parameters
    for param in OPENROUTER_SPECIFIC_PARAMS:
        if param in ollama_payload:
            openrouter_payload[param] = ollama_payload[param]
    
    return openrouter_payload


async def openrouter_stream_to_ollama_stream(
    openrouter_stream: AsyncGenerator[str, None], 
    model_name: Optional[str]
) -> AsyncGenerator[bytes, None]:
    """
    Translates an OpenRouter/OpenAI SSE stream into an Ollama-compatible SSE stream.
    OpenRouter uses the same format as OpenAI, so this is identical to vLLM.
    """
    start_time = time.monotonic()
    total_eval_text = ""
    buffer = ""

    def get_iso_timestamp(ts: int | None) -> str:
        """Converts a Unix timestamp to an ISO 8601 string, ensuring Z-suffix for UTC."""
        if ts is None:
            dt_obj = datetime.now(timezone.utc)
        else:
            dt_obj = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt_obj.isoformat().replace('+00:00', 'Z')

    async for text_chunk in openrouter_stream:
        buffer += text_chunk
        lines = buffer.split('\n')
        buffer = lines.pop()  # Keep any partial line for the next chunk

        for line in lines:
            if not line.strip():
                continue

            if line.strip() == "data: [DONE]":
                end_time = time.monotonic()
                eval_duration_ns = (end_time - start_time) * 1_000_000_000
                eval_count = len(total_eval_text) // 4
                
                final_done_chunk = { 
                    "model": model_name or "openrouter",
                    "created_at": get_iso_timestamp(None),
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "eval_count": eval_count,
                    "eval_duration": int(eval_duration_ns)
                }
                yield (json.dumps(final_done_chunk) + '\n').encode('utf-8')
                return  # End of stream

            if not line.startswith("data: "):
                continue

            try:
                data_str = line.lstrip("data: ").strip()
                if not data_str:
                    continue
                
                data = json.loads(data_str)
                delta = data.get("choices", [{}])[0].get("delta", {})
                finish_reason = data.get("choices", [{}])[0].get("finish_reason")
                created_ts = data.get("created")
                
                # Get the actual model used (for auto-routing)
                actual_model = data.get("model", model_name)

                # Handle regular content
                if content := delta.get("content"):
                    total_eval_text += content
                    ollama_chunk = {
                        "model": actual_model or "openrouter",
                        "created_at": get_iso_timestamp(created_ts),
                        "message": {"role": "assistant", "content": content},
                        "done": False,
                    }
                    yield (json.dumps(ollama_chunk) + '\n').encode('utf-8')

            except (json.JSONDecodeError, IndexError) as e:
                logger.warning(f"Could not parse OpenRouter stream chunk: {line}. Error: {e}")
                continue
    
    # Process any final data left in the buffer
    if buffer.strip():
        line = buffer.strip()
        if line.strip() == "data: [DONE]":
            end_time = time.monotonic()
            eval_duration_ns = (end_time - start_time) * 1_000_000_000
            eval_count = len(total_eval_text) // 4
            final_done_chunk = { 
                "model": model_name or "openrouter",
                "created_at": get_iso_timestamp(None),
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "eval_count": eval_count,
                "eval_duration": int(eval_duration_ns)
            }
            yield (json.dumps(final_done_chunk) + '\n').encode('utf-8')


def translate_openrouter_to_ollama_embeddings(openrouter_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Translates an OpenRouter embeddings response to Ollama format.
    """
    embedding_data = openrouter_payload.get("data", [])
    embedding = embedding_data[0].get("embedding") if embedding_data else []
    return {"embedding": embedding}


def get_openrouter_headers(server_api_key: Optional[str], extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Builds headers for OpenRouter requests.
    Includes Authorization and optional HTTP-Referer/X-Title headers.
    """
    headers = {
        "Content-Type": "application/json",
    }
    
    if server_api_key:
        headers["Authorization"] = f"Bearer {server_api_key}"
    
    # Add optional OpenRouter headers if provided
    if extra_headers:
        if "HTTP-Referer" in extra_headers:
            headers["HTTP-Referer"] = extra_headers["HTTP-Referer"]
        if "X-Title" in extra_headers:
            headers["X-Title"] = extra_headers["X-Title"]
    
    return headers

