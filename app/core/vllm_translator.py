import json
import logging
from typing import Dict, Any, AsyncGenerator
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# --- Constants for Chain-of-Thought ---
CHAIN_OF_THOUGHT_PROMPT = "When you are asked a question, first provide a step-by-step plan of how you will answer the question inside  tags. After the closing </think> tag, produce the final answer."

THINK_TOOL = {
    "type": "function",
    "function": {
        "name": "think",
        "description": "Elaborate on the reasoning process, step-by-step, before providing the final answer.",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "An array of strings, where each string is a step in the reasoning process.",
                }
            },
            "required": ["steps"],
        },
    },
}

# --- Request Translation ---
def translate_ollama_to_vllm_chat(ollama_payload: Dict[str, Any]) -> Dict[str, Any]:
    vllm_payload = {
        "model": ollama_payload.get("model"),
        "stream": ollama_payload.get("stream", False),
    }
    
    messages = ollama_payload.get("messages", [])
    
    # Check for and handle Chain-of-Thought prompt for vLLM
    final_messages = []
    
    is_thinking_on = ollama_payload.get("think") is True

    # Inject CoT prompt if thinking is enabled for a non-native model
    if is_thinking_on:
        has_system_prompt = False
        for message in messages:
            if message.get("role") == "system":
                message["content"] = f"{CHAIN_OF_THOUGHT_PROMPT}\n\n{message.get('content', '')}".strip()
                has_system_prompt = True
            final_messages.append(message)
        if not has_system_prompt:
            final_messages.insert(0, {"role": "system", "content": CHAIN_OF_THOUGHT_PROMPT})
    else:
        final_messages = messages

    vllm_payload["messages"] = final_messages

    # Translate image format if present
    for message in vllm_payload["messages"]:
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
            
    return vllm_payload

def translate_ollama_to_vllm_embeddings(ollama_payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model": ollama_payload.get("model"),
        "input": ollama_payload.get("prompt"),
    }

# --- Response Translation ---
async def vllm_stream_to_ollama_stream(vllm_stream: AsyncGenerator[str, None], model_name: str) -> AsyncGenerator[bytes, None]:
    """
    Translates a vLLM/OpenAI SSE stream into an Ollama-compatible SSE stream.
    Handles regular content and tool calls for "thinking".
    """
    tool_call_buffer = ""
    in_tool_call = False
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

    async for text_chunk in vllm_stream:
        buffer += text_chunk
        lines = buffer.split('\n')
        buffer = lines.pop() # Keep any partial line for the next chunk

        for line in lines:
            if not line.strip():
                continue

            if line.strip() == "data: [DONE]":
                end_time = time.monotonic()
                eval_duration_ns = (end_time - start_time) * 1_000_000_000
                eval_count = len(total_eval_text) // 4
                
                final_done_chunk = { 
                    "model": model_name,
                    "created_at": get_iso_timestamp(None),
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "eval_count": eval_count,
                    "eval_duration": int(eval_duration_ns)
                }
                yield (json.dumps(final_done_chunk) + '\n').encode('utf-8')
                return # End of stream, stop the generator.

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

                # --- Handle Tool Call for "thinking" ---
                if "tool_calls" in delta:
                    if not in_tool_call:
                        in_tool_call = True
                        # Yield <think> start tag as a separate message
                        start_think_chunk = {
                            "model": model_name, "created_at": get_iso_timestamp(created_ts),
                            "message": {"role": "assistant", "content": "<think>"}, "done": False
                        }
                        yield (json.dumps(start_think_chunk) + '\n').encode('utf-8')
                    
                    tool_call_part = delta["tool_calls"][0].get("function", {}).get("arguments", "")
                    if tool_call_part:
                        tool_call_buffer += tool_call_part
                
                # --- Process completed tool call ---
                if in_tool_call and finish_reason == "tool_calls":
                    try:
                        args = json.loads(tool_call_buffer)
                        steps = args.get("steps", [])
                        thinking_content = '\n'.join(steps) if isinstance(steps, list) else str(steps)
                        total_eval_text += thinking_content
                        
                        # Yield the content of the thought
                        ollama_chunk = {
                            "model": model_name, "created_at": get_iso_timestamp(created_ts),
                            "message": {"role": "assistant", "content": thinking_content}, "done": False,
                        }
                        yield (json.dumps(ollama_chunk) + '\n').encode('utf-8')
                        
                        # Yield the closing </think> tag
                        end_think_chunk = {
                            "model": model_name, "created_at": get_iso_timestamp(created_ts),
                            "message": {"role": "assistant", "content": "</think>"}, "done": False
                        }
                        yield (json.dumps(end_think_chunk) + '\n').encode('utf-8')
                        
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse tool call arguments: {tool_call_buffer}. Error: {e}")
                    
                    tool_call_buffer = ""
                    in_tool_call = False
                    if "content" not in delta or not delta.get("content"):
                        continue

                # --- Handle regular content ---
                if content := delta.get("content"):
                    total_eval_text += content
                    ollama_chunk = {
                        "model": model_name, "created_at": get_iso_timestamp(created_ts),
                        "message": {"role": "assistant", "content": content}, "done": False,
                    }
                    yield (json.dumps(ollama_chunk) + '\n').encode('utf-8')

            except (json.JSONDecodeError, IndexError) as e:
                logger.warning(f"Could not parse VLLM stream chunk: {line}. Error: {e}")
                continue
    
    # Process any final data left in the buffer. This is a safeguard.
    if buffer.strip():
        line = buffer.strip()
        if line.strip() == "data: [DONE]":
            end_time = time.monotonic()
            eval_duration_ns = (end_time - start_time) * 1_000_000_000
            eval_count = len(total_eval_text) // 4
            final_done_chunk = { 
                "model": model_name,
                "created_at": get_iso_timestamp(None),
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "eval_count": eval_count,
                "eval_duration": int(eval_duration_ns)
            }
            yield (json.dumps(final_done_chunk) + '\n').encode('utf-8')


def translate_vllm_to_ollama_embeddings(vllm_payload: Dict[str, Any]) -> Dict[str, Any]:
    embedding_data = vllm_payload.get("data", [])
    embedding = embedding_data[0].get("embedding") if embedding_data else []
    return {"embedding": embedding}