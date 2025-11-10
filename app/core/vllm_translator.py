# app/core/vllm_translator.py
import json
import datetime
from typing import Dict, Any, AsyncGenerator, List

def translate_ollama_to_vllm_chat(ollama_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Translates an Ollama chat request to a vLLM/OpenAI compatible one."""
    vllm_payload = ollama_payload.copy()
    
    for message in vllm_payload.get("messages", []):
        if "images" in message and message["images"]:
            text_content = message.get("content", "")
            new_content: List[Dict[str, Any]] = [{"type": "text", "text": text_content}]
            for img_b64 in message["images"]:
                # We can't know the image type from base64, so we default to jpeg.
                # This is a common practice.
                new_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                })
            message["content"] = new_content
            del message["images"]
            
    return vllm_payload

def translate_ollama_to_vllm_embeddings(ollama_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Translates an Ollama embeddings request to a vLLM/OpenAI compatible one."""
    vllm_payload = ollama_payload.copy()
    if "prompt" in vllm_payload:
        vllm_payload["input"] = vllm_payload.pop("prompt")
    return vllm_payload

def translate_vllm_to_ollama_embeddings(vllm_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Translates a vLLM/OpenAI embeddings response to the Ollama format."""
    embedding_data = vllm_payload.get("data", [{}])[0]
    return {"embedding": embedding_data.get("embedding", [])}


async def vllm_stream_to_ollama_stream(
    vllm_response_iterator: AsyncGenerator[str, None], 
    model_name: str
) -> AsyncGenerator[bytes, None]:
    """
    Translates a vLLM/OpenAI SSE stream to Ollama's ndjson stream format.
    """
    full_response_content = ""
    async for chunk in vllm_response_iterator:
        for line in chunk.strip().split('\n'):
            if not line.startswith("data:"):
                continue
            
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break

            try:
                data = json.loads(data_str)
                delta = data.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content")
                if content:
                    full_response_content += content
                    ollama_chunk = {
                        "model": model_name,
                        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
                        "message": {"role": "assistant", "content": content},
                        "done": False
                    }
                    yield (json.dumps(ollama_chunk) + '\n').encode('utf-8')
            except (json.JSONDecodeError, IndexError):
                continue
    
    # Send the final 'done' message with stats
    final_chunk = {
        "model": model_name,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "message": {"role": "assistant", "content": full_response_content},
        "done": True,
        "total_duration": 0, "load_duration": 0, "prompt_eval_count": 0, 
        "prompt_eval_duration": 0, "eval_count": 0, "eval_duration": 0
    }
    yield (json.dumps(final_chunk) + '\n').encode('utf-8')
