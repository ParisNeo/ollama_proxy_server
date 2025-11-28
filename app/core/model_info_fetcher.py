"""
Fetch model information from OpenRouter and Ollama APIs
"""
import logging
import httpx
from typing import Dict, Any, Optional
from app.core.openrouter_translator import OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)


async def fetch_openrouter_model_info(api_key: str, model_name: str) -> Optional[Dict[str, Any]]:
    """
    Fetch detailed model information from OpenRouter API
    
    Args:
        api_key: OpenRouter API key
        model_name: Model name/ID (e.g., "openai/gpt-4o-search-preview")
        
    Returns:
        Dict with model details or None if failed
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{OPENROUTER_BASE_URL}/models",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
            )
            response.raise_for_status()
            data = response.json()
            
            # Find the specific model in the list
            models = data.get("data", [])
            for model in models:
                if model.get("id") == model_name or model.get("name") == model_name:
                    return model
            
            logger.warning(f"Model '{model_name}' not found in OpenRouter models list")
            return None
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenRouter model info HTTP error: {e.response.status_code} - {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch OpenRouter model info: {e}", exc_info=True)
        return None


async def fetch_ollama_model_info(server_url: str, model_name: str, api_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Fetch detailed model information from Ollama API
    
    Args:
        server_url: Ollama server URL (e.g., "http://localhost:11434")
        model_name: Model name (e.g., "gemma3")
        api_key: Optional API key for authentication
        
    Returns:
        Dict with model details or None if failed
    """
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            # First try /api/show for detailed info
            show_response = await client.post(
                f"{server_url.rstrip('/')}/api/show",
                json={"model": model_name, "verbose": True},
                headers=headers
            )
            
            if show_response.status_code == 200:
                show_data = show_response.json()
                return {
                    "name": model_name,
                    "details": show_data.get("details", {}),
                    "capabilities": show_data.get("capabilities", []),
                    "context_length": show_data.get("model_info", {}).get("general.context_length") or 
                                     show_data.get("details", {}).get("context_length"),
                    "parameter_size": show_data.get("details", {}).get("parameter_size"),
                    "family": show_data.get("details", {}).get("family"),
                    "families": show_data.get("details", {}).get("families", []),
                    "license": show_data.get("license"),
                    "template": show_data.get("template"),
                    "model_info": show_data.get("model_info", {})
                }
            
            # Fallback to /api/tags for basic info
            tags_response = await client.get(
                f"{server_url.rstrip('/')}/api/tags",
                headers=headers
            )
            
            if tags_response.status_code == 200:
                tags_data = tags_response.json()
                models = tags_data.get("models", [])
                for model in models:
                    if model.get("name") == model_name:
                        return {
                            "name": model_name,
                            "details": model.get("details", {}),
                            "size": model.get("size"),
                            "modified_at": model.get("modified_at"),
                            "context_length": model.get("details", {}).get("context_length"),
                            "parameter_size": model.get("details", {}).get("parameter_size"),
                            "family": model.get("details", {}).get("family"),
                            "families": model.get("details", {}).get("families", [])
                        }
            
            logger.warning(f"Model '{model_name}' not found in Ollama server")
            return None
    except httpx.HTTPStatusError as e:
        logger.error(f"Ollama model info HTTP error: {e.response.status_code} - {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch Ollama model info: {e}", exc_info=True)
        return None


async def get_model_info_from_server(
    db,
    model_name: str,
    server_crud
) -> Optional[Dict[str, Any]]:
    """
    Fetch model information from the appropriate server (OpenRouter or Ollama)
    
    Args:
        db: Database session
        model_name: Model name to fetch info for
        server_crud: Server CRUD instance
        
    Returns:
        Dict with model details or None if failed
    """
    servers = await server_crud.get_servers(db)
    active_servers = [s for s in servers if s.is_active]
    
    # Check if model is from OpenRouter
    for server in active_servers:
        if server.server_type == "openrouter" and server.available_models:
            import json
            models_list = server.available_models
            if isinstance(models_list, str):
                try:
                    models_list = json.loads(models_list)
                except:
                    continue
            
            for model_data in models_list:
                if isinstance(model_data, dict) and model_data.get("name") == model_name:
                    # Found in OpenRouter, fetch detailed info
                    from app.core.encryption import decrypt_data
                    api_key = decrypt_data(server.encrypted_api_key) if server.encrypted_api_key else None
                    if api_key:
                        return await fetch_openrouter_model_info(api_key, model_name)
                    break
    
    # Check if model is from Ollama
    for server in active_servers:
        if server.server_type == "ollama":
            from app.core.encryption import decrypt_data
            api_key = decrypt_data(server.encrypted_api_key) if server.encrypted_api_key else None
            model_info = await fetch_ollama_model_info(server.url, model_name, api_key)
            if model_info:
                return model_info
    
    return None

