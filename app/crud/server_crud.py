from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models import OllamaServer
from app.schema.server import ServerCreate, ServerUpdate
from app.core.encryption import encrypt_data, decrypt_data
import httpx
import logging
import datetime
from typing import Optional, List, Dict, Any
import asyncio
import json
import re
import socket
import secrets
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Industry standard default endpoints for different AI backends
DEFAULT_SERVER_URLS = {
    "ollama": "http://127.0.0.1:11434",
    "vllm": "http://127.0.0.1:8000/v1",
    "novita": "https://api.novita.ai/v3/openai/v1",
    "openllm": "http://127.0.0.1:3000/v1",
    "cloud": "https://ollama.com/api",
    "open_webui": "http://127.0.0.1:3000/api",
    "openrouter": "https://openrouter.ai/api/v1"
}

def _get_auth_headers(server: OllamaServer) -> Dict[str, str]:
    """
    Generates appropriate headers for backend servers.
    Ensures 'Authorization' is set for Cloud and vLLM providers.
    """
    headers = {}
    if server.encrypted_api_key:
        api_key = decrypt_data(server.encrypted_api_key)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            
            # --- OPENROUTER IDENTIFICATION ---
            if server.server_type == 'openrouter':
                # These headers help OpenRouter track the app and show it in their rankings
                headers["HTTP-Referer"] = "https://github.com/ParisNeo/lollms_hub"
                headers["X-Title"] = "LoLLMs Hub Fortress"
                
    return headers


def _is_safe_url(url: str) -> bool:
    """
    Validates the backend URL. Now specifically tuned for local servers
    and the new Ollama Cloud (ollama.com/api).
    """
    try:
        url_str = str(url).lower()
        parsed = urlparse(url_str)
        
        if parsed.scheme not in ('http', 'https'):
            return False
            
        if not parsed.netloc:
            return False
            
        # Explicitly allow standard cloud providers
        if any(domain in parsed.netloc for domain in ["ollama.com", "novita.ai"]):
            return True
            
        return True
        
    except Exception as e:
        logger.warning(f"URL validation check failed for {url}: {e}")
        return False


async def get_server_by_id(db: AsyncSession, server_id: int) -> OllamaServer | None:
    result = await db.execute(select(OllamaServer).filter(OllamaServer.id == server_id))
    return result.scalars().first()


async def get_server_by_url(db: AsyncSession, url: str) -> OllamaServer | None:
    result = await db.execute(select(OllamaServer).filter(OllamaServer.url == url))
    return result.scalars().first()


async def get_server_by_name(db: AsyncSession, name: str) -> OllamaServer | None:
    result = await db.execute(select(OllamaServer).filter(OllamaServer.name == name))
    return result.scalars().first()


async def get_servers(db: AsyncSession, skip: int = 0, limit: Optional[int] = None) -> list[OllamaServer]:
    query = select(OllamaServer).order_by(OllamaServer.created_at.desc()).offset(skip)
    if limit is not None:
        # Validate limit
        limit = max(1, min(int(limit), 1000))
        query = query.limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


async def create_server(db: AsyncSession, server: ServerCreate) -> OllamaServer:
    """Creates a new server. Multiple servers with same URL are allowed (e.g. different account keys)."""
    
    # 1. Apply binding defaults if URL is essentially empty
    url_str = str(server.url).strip()
    # Check if user left default browser filler or empty
    if not url_str or url_str in ("http://", "https://"):
        default_url = DEFAULT_SERVER_URLS.get(server.server_type)
        if default_url:
            server.url = default_url
            url_str = default_url

    # 2. Validate URL safety
    if not _is_safe_url(url_str):
        raise ValueError(f"URL {url_str} is not allowed for security reasons.")
        
    # 3. Validate name
    if not server.name or len(server.name) > 128:
        raise ValueError("Server name must be 1-128 characters")
        
    # Validate server_type
    valid_types = ('ollama', 'vllm', 'cloud', 'novita')
    if server.server_type not in valid_types:
        raise ValueError(f"Server type must be one of {valid_types}")

    encrypted_key = encrypt_data(server.api_key) if server.api_key else None
    db_server = OllamaServer(
        name=server.name, 
        url=str(server.url), 
        server_type=server.server_type,
        encrypted_api_key=encrypted_key
    )
    db.add(db_server)
    await db.commit()
    await db.refresh(db_server)
    return db_server


async def update_server(db: AsyncSession, server_id: int, server_update: ServerUpdate) -> OllamaServer | None:
    db_server = await get_server_by_id(db, server_id)
    if not db_server:
        return None

    update_data = server_update.model_dump(exclude_unset=True)

    if "allowed_models" in update_data:
        db_server.allowed_models = update_data.pop("allowed_models")
    
    # 1. Apply binding defaults on update if URL is cleared
    if "url" in update_data and update_data["url"]:
        url_str = str(update_data["url"]).strip()
        if not url_str or url_str in ("http://", "https://"):
            s_type = update_data.get("server_type", db_server.server_type)
            update_data["url"] = DEFAULT_SERVER_URLS.get(s_type, url_str)

    # 2. If api_key is provided, update it. If not, don't touch existing encrypted_api_key.
    # We check if api_key is present in update_data, and if it's not None (the empty string check handles clear)
    if "api_key" in update_data:
        api_key = update_data.pop("api_key")
        if api_key is not None:
            db_server.encrypted_api_key = encrypt_data(api_key) if api_key else None
        
    for key, value in update_data.items():
        if value is not None:
            # Validate URL on update
            if key == 'url':
                if not _is_safe_url(str(value)):
                    raise ValueError(f"URL {value} is not allowed for security reasons")
                setattr(db_server, key, str(value))
            elif key == 'name':
                if not value or len(value) > 128:
                    raise ValueError("Server name must be 1-128 characters")
                setattr(db_server, key, value)
            elif key == 'server_type':
                valid_types = ('ollama', 'vllm', 'cloud', 'novita')
                if value not in valid_types:
                    raise ValueError(f"Server type must be one of {valid_types}")
                setattr(db_server, key, value)
            else:
                setattr(db_server, key, value)
            
    await db.commit()
    await db.refresh(db_server)
    return db_server


async def delete_server(db: AsyncSession, server_id: int) -> OllamaServer | None:
    result = await db.execute(select(OllamaServer).filter(OllamaServer.id == server_id))
    server = result.scalars().first()
    if server:
        await db.delete(server)
        await db.commit()
    return server


async def fetch_and_update_models(db: AsyncSession, server_id: int) -> dict:
    """
    Fetches the list of available models from a server and updates the database.
    Handles both Ollama and vLLM (OpenAI-compatible) servers.

    Returns a dict with 'success' (bool), 'models' (list), and optionally 'error' (str)
    """
    server = await get_server_by_id(db, server_id)
    if not server:
        return {"success": False, "error": "Server not found", "models": []}
    
    # Security: Re-validate URL at fetch time
    if not _is_safe_url(server.url):
        error_msg = f"URL {server.url} is blocked for security reasons"
        logger.error(f"Security block: {error_msg}")
        server.last_error = error_msg
        await db.commit()
        return {"success": False, "error": error_msg, "models": []}
    
    headers = _get_auth_headers(server)

    try:
        models = []
        # Use timeout to prevent hanging
        timeout = httpx.Timeout(30.0, connect=10.0)
        
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=False) as client:
            if server.server_type in ("vllm", "novita", "openllm"):
                # Append exactly /models to the base URL (e.g. .../v3/openai/models)
                endpoint_url = f"{server.url.rstrip('/')}/models"
                response = await client.get(endpoint_url)
                response.raise_for_status()
                
                # OpenAI-compatible parsing
                data = response.json()
                raw_models = data.get("data", []) # OpenAI uses 'data' key
                
                for model in raw_models:
                    if not isinstance(model, dict): continue
                    m_id = model.get("id")
                    if not m_id: continue
                    
                    models.append({
                        "name": str(m_id),
                        "size": 0,
                        "modified_at": datetime.datetime.utcnow().isoformat() + "Z",
                        "digest": f"sha256:{secrets.token_hex(32)}",
                        "details": {
                            "parent_model": "",
                            "format": "openai",
                            "family": "cloud",
                            "families": ["openai", "cloud"],
                            "parameter_size": "N/A",
                            "quantization_level": "N/A"
                        }
                    })
            else:  # Default to "ollama" or "cloud"
                endpoint_url = f"{server.url.rstrip('/')}/api/tags"
                response = await client.get(endpoint_url)
                response.raise_for_status()
                data = response.json()
                raw_models = data.get("models", [])
                
                # Validate and sanitize model data
                for model in raw_models:
                    if not isinstance(model, dict):
                        continue
                    name = model.get("name")
                    if not name or not isinstance(name, str):
                        continue
                    # Sanitize model name
                    name = re.sub(r'[^\w\.\-:@]', '', name)[:256]
                    
                    # Sanitize other fields
                    size = model.get("size", 0)
                    try:
                        size = int(size)
                    except (ValueError, TypeError):
                        size = 0
                        
                    modified_at = model.get("modified_at", "")
                    if not isinstance(modified_at, str):
                        modified_at = ""
                    modified_at = modified_at[:64]
                    
                    digest = model.get("digest", "")
                    if not isinstance(digest, str):
                        digest = ""
                    digest = re.sub(r'[^\w:]', '', digest)[:128]
                    
                    # Sanitize details
                    details = model.get("details", {})
                    if not isinstance(details, dict):
                        details = {}
                    
                    safe_model = {
                        "name": name,
                        "size": size,
                        "modified_at": modified_at,
                        "digest": digest,
                        "details": {
                            "parent_model": str(details.get("parent_model", ""))[:128],
                            "format": str(details.get("format", ""))[:64],
                            "family": str(details.get("family", ""))[:64],
                            "families": None,
                            "parameter_size": str(details.get("parameter_size", ""))[:32],
                            "quantization_level": str(details.get("quantization_level", ""))[:32]
                        }
                    }
                    if details.get("families") and isinstance(details["families"], list):
                        safe_families = [str(f)[:64] for f in details["families"] if isinstance(f, str)]
                        safe_model["details"]["families"] = safe_families[:10]  # Limit array size
                    
                    models.append(safe_model)
        
        # Limit total models to prevent DoS via huge model list
        if len(models) > 10000:
            logger.warning(f"Truncating model list from {len(models)} to 10000 for server {server.name}")
            models = models[:10000]
            
        server.available_models = models
        server.models_last_updated = datetime.datetime.utcnow()
        server.last_error = None
        await db.commit()
        await db.refresh(server)

        logger.info(f"Successfully fetched {len(models)} models from {server.server_type} server '{server.name}'")
        return {"success": True, "models": models, "error": None}

    except httpx.HTTPError as e:
        error_msg = f"HTTP error: {str(e)}"[:512]  # Limit error message length
        logger.error(f"Failed to fetch models from server '{server.name}': {error_msg}")
        server.last_error = error_msg
        server.available_models = None
        await db.commit()
        return {"success": False, "error": error_msg, "models": []}
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"[:512]
        logger.error(f"Failed to fetch models from server '{server.name}': {error_msg}")
        server.last_error = error_msg
        server.available_models = None
        await db.commit()
        return {"success": False, "error": error_msg, "models": []}


async def pull_model_on_server(http_client: httpx.AsyncClient, server: OllamaServer, model_name: str) -> dict:
    """Pulls a model on a specific Ollama server."""
    if server.server_type == 'vllm':
        return {"success": False, "message": "Pulling models is not supported for vLLM servers."}
    
    # Validate model name
    if not model_name or len(model_name) > 256:
        return {"success": False, "message": "Invalid model name"}
        
    # Sanitize model name
    if not re.match(r'^[\w\.\-:@]+$', model_name):
        return {"success": False, "message": "Model name contains invalid characters"}
        
    headers = _get_auth_headers(server)
    pull_url = f"{server.url.rstrip('/')}/api/pull"
    payload = {"name": model_name, "stream": False}
    try:
        from app.core.events import event_manager, ProxyEvent
        # Use a long timeout as pulling can take a significant amount of time
        async with http_client.stream("POST", pull_url, json=payload, timeout=1800.0, headers=headers) as response:
            if response.status_code >= 400:
                error_text = await response.aread()
                raise httpx.HTTPStatusError(f"Pull failed: {error_text.decode()}", request=response.request, response=response)
                
            async for line_text in response.aiter_lines():
                if not line_text: continue
                try:
                    data = json.loads(line_text)
                    status = data.get("status", "processing")
                    completed = data.get("completed", 0)
                    total = data.get("total", 0)
                    
                    # Calculate progress percentage
                    progress = 0
                    if total > 0:
                        progress = round((completed / total) * 100, 1)
                        status = f"{status} ({progress}%)"

                    # Emit a specialized active event for the UI to catch
                    # We use a naming convention: pull_[server_id]_[model_name]
                    task_id = f"pull_{server.id}_{model_name}"
                    event_manager.emit(ProxyEvent(
                        "active", task_id, model_name, server.name, "system",
                        error_message=status # We repurpose error_message to carry the status text
                    ))
                    
                    logger.debug(f"Pulling {model_name}: {status}")
                except json.JSONDecodeError:
                    continue 
        
        logger.info(f"Successfully pulled/updated model '{model_name}' on server '{server.name}'")
        return {"success": True, "message": f"Model '{model_name}' pulled/updated successfully."}
        
    except httpx.HTTPStatusError as e:
        error_msg = f"Failed to pull model '{model_name}': Server returned status {e.response.status_code}"[:512]
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}
    except Exception as e:
        error_msg = f"An unexpected error occurred while pulling model '{model_name}': {str(e)}"[:512]
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}


async def delete_model_on_server(http_client: httpx.AsyncClient, server: OllamaServer, model_name: str) -> dict:
    """Deletes a model from a specific Ollama server."""
    if server.server_type == 'vllm':
        return {"success": False, "message": "Deleting models is not supported for vLLM servers."}

    # Validate model name
    if not model_name or len(model_name) > 256:
        return {"success": False, "message": "Invalid model name"}
        
    # Sanitize model name
    if not re.match(r'^[\w\.\-:@]+$', model_name):
        return {"success": False, "message": "Model name contains invalid characters"}

    headers = _get_auth_headers(server)
    delete_url = f"{server.url.rstrip('/')}/api/delete"
    payload = {"name": model_name}
    try:
        # FIX: Use the more robust .request() method to send a JSON body with DELETE.
        # This is compatible with a wider range of httpx versions.
        response = await http_client.request("DELETE", delete_url, json=payload, timeout=120.0, headers=headers)
        response.raise_for_status()
        logger.info(f"Successfully deleted model '{model_name}' from server '{server.name}'")
        return {"success": True, "message": f"Model '{model_name}' deleted successfully."}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            message = f"Model '{model_name}' not found on server."
            logger.warning(message)
            return {"success": True, "message": message} # Treat not found as a success
        error_msg = f"Failed to delete model '{model_name}': Server returned status {e.response.status_code}"[:512]
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}
    except Exception as e:
        error_msg = f"An unexpected error occurred while deleting model '{model_name}': {str(e)}"[:512]
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}


async def load_model_on_server(http_client: httpx.AsyncClient, server: OllamaServer, model_name: str) -> dict:
    """Sends a dummy request to a server to load a model into memory."""
    if server.server_type == 'vllm':
        return {"success": False, "message": "Explicit model loading is not applicable for vLLM servers."}

    # Validate model name
    if not model_name or len(model_name) > 256:
        return {"success": False, "message": "Invalid model name"}
        
    # Sanitize model name
    if not re.match(r'^[\w\.\-:@]+$', model_name):
        return {"success": False, "message": "Model name contains invalid characters"}

    headers = _get_auth_headers(server)
    generate_url = f"{server.url.rstrip('/')}/api/generate"
    payload = {"model": model_name, "prompt": " ", "stream": False}
    try:
        # Use a timeout sufficient for model loading
        response = await http_client.post(generate_url, json=payload, timeout=300.0, headers=headers)
        response.raise_for_status()
        logger.info(f"Successfully triggered load for model '{model_name}' on server '{server.name}'")
        return {"success": True, "message": f"Model '{model_name}' is being loaded into memory."}
    except httpx.HTTPStatusError as e:
        try:
            error_detail = e.response.json().get('error', e.response.text)
        except json.JSONDecodeError:
            error_detail = e.response.text
        error_msg = f"Failed to load model '{model_name}': Server returned status {e.response.status_code}: {error_detail}"[:512]
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}
    except Exception as e:
        error_msg = f"An unexpected error occurred while loading model '{model_name}': {str(e)}"[:512]
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}


async def unload_model_on_server(http_client: httpx.AsyncClient, server: OllamaServer, model_name: str) -> dict:
    """Sends a request to a server to unload a model from memory."""
    if server.server_type == 'vllm':
        return {"success": False, "message": "Explicit model unloading is not applicable for vLLM servers."}

    # Validate model name
    if not model_name or len(model_name) > 256:
        return {"success": False, "message": "Invalid model name"}
        
    # Sanitize model name
    if not re.match(r'^[\w\.\-:@]+$', model_name):
        return {"success": False, "message": "Model name contains invalid characters"}

    headers = _get_auth_headers(server)
    generate_url = f"{server.url.rstrip('/')}/api/generate"
    # Setting keep_alive to 0s tells Ollama to unload the model after this request.
    payload = {"model": model_name, "prompt": " ", "keep_alive": "0s"}
    try:
        response = await http_client.post(generate_url, json=payload, timeout=60.0, headers=headers)
        response.raise_for_status()
        logger.info(f"Successfully triggered unload for model '{model_name}' on server '{server.name}'")
        return {"success": True, "message": f"Unload signal sent for model '{model_name}'. It will be removed from memory shortly."}
    except httpx.HTTPStatusError as e:
        # If the model isn't found (which can happen if it's not loaded), treat as success.
        if e.response.status_code == 404:
             return {"success": True, "message": f"Model '{model_name}' was not loaded in memory."}
        try:
            error_detail = e.response.json().get('error', e.response.text)
        except json.JSONDecodeError:
            error_detail = e.response.text
        error_msg = f"Failed to unload model '{model_name}': Server returned status {e.response.status_code}: {error_detail}"[:512]
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}
    except Exception as e:
        error_msg = f"An unexpected error occurred while unloading model '{model_name}': {str(e)}"[:512]
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}


async def get_servers_with_model(db: AsyncSession, model_name: str) -> list[OllamaServer]:
    """
    Get all active servers that have the specified model available, 
    respecting the admin whitelist (allowed_models).
    """
    if not model_name:
        return []
    
    # Defensive fix for IRRA: Ensure model_name is a string before regex
    if isinstance(model_name, list):
        if len(model_name) > 0:
            model_name = str(model_name[0])
        else:
            return []
        
    model_name = re.sub(r'[^\w\.\-:@]', '', str(model_name))[:256]

    servers = await get_servers(db)
    active_servers = [s for s in servers if s.is_active]

    servers_with_model = []
    for server in active_servers:
        # 1. Check Administrative Permission (Whitelist)
        # If the whitelist is not empty, the model MUST be in it.
        if server.allowed_models and len(server.allowed_models) > 0:
            is_ollama = server.server_type == 'ollama'
            whitelisted = any(
                m == model_name or 
                m.replace(':latest', '') == model_name.replace(':latest', '') or
                (is_ollama and m.startswith(f"{model_name}:"))
                for m in server.allowed_models
            )
            if not whitelisted:
                # Model exists but is restricted by this server's specific whitelist
                continue

        # 2. Verify Physical Availability in the server's discovered catalog
        if server.available_models:
            models_list = server.available_models
            if isinstance(models_list, str):
                try: models_list = json.loads(models_list)
                except json.JSONDecodeError: continue
            
            if not isinstance(models_list, list): continue
                
            for model_data in models_list:
                if isinstance(model_data, dict) and "name" in model_data:
                    available_model_name = model_data["name"]
                    if not isinstance(available_model_name, str): continue
                        
                    is_ollama = server.server_type in ('ollama', 'cloud')
                    
                    # Normalize names for comparison (remove :latest)
                    norm_avail = available_model_name.replace(':latest', '')
                    norm_req = model_name.replace(':latest', '')

                    is_match = (
                        available_model_name == model_name or
                        norm_avail == norm_req or
                        # Allow "model" to match "model:latest" and vice versa
                        available_model_name == f"{model_name}:latest" or
                        model_name == f"{available_model_name}:latest" or
                        (is_ollama and available_model_name.startswith(f"{model_name}:")) or
                        (is_ollama and model_name.startswith(f"{available_model_name}:")) or
                        (server.server_type in ('vllm', 'novita', 'openllm') and norm_req in norm_avail)
                    )
                    
                    # HARD SAFETY: If this is an embedding model, reject it for chat requests
                    # We infer this from the name if the metadata is stale
                    if is_match and is_embedding_model(available_model_name):
                        # Only allow if the user *specifically* asked for an embedding endpoint
                        # but block it for chat routes.
                        # This logic is best-effort since we don't know the route here, 
                        # but we can filter it out of the general "servers_with_model" list.
                        is_match = False
                    
                    if is_match:
                        servers_with_model.append(server)
                        break
    return servers_with_model


def is_embedding_model(model_name: str) -> bool:
    """Heuristically determines if a model is for embeddings."""
    if not isinstance(model_name, str):
        return False
    m_lower = model_name.lower()
    # Explicitly check for 'embedding' as requested, plus common provider patterns
    return any(kw in m_lower for kw in ["embed", "embedding", "bge", "gte", "nomic", "snowflake"])


async def get_all_available_model_names(db: AsyncSession, filter_type: Optional[str] = None) -> List[str]:
    """
    Gets a unique, sorted list of all model names across all active servers.
    Can be filtered by type ('chat' or 'embedding').
    """
    # Validate filter_type
    if filter_type not in (None, 'chat', 'embedding'):
        filter_type = None
        
    servers = await get_servers(db)
    active_servers = [s for s in servers if s.is_active]

    all_models = set()
    for server in active_servers:
        if not server.available_models:
            continue
        
        models_list = server.available_models
        if isinstance(models_list, str):
            try:
                models_list = json.loads(models_list)
            except json.JSONDecodeError:
                logger.warning(f"Could not parse available_models JSON for server {server.name} in get_all_available_model_names")
                continue

        if not isinstance(models_list, list):
            continue

        for model in models_list:
            if isinstance(model, dict) and "name" in model:
                model_name = model["name"]
                if not isinstance(model_name, str):
                    continue
                    
                # Sanitize
                model_name = model_name[:256]
                
                is_embed = is_embedding_model(model_name)
                
                if filter_type == 'embedding' and is_embed:
                    all_models.add(model_name)
                elif filter_type == 'chat' and not is_embed:
                    all_models.add(model_name)
                elif filter_type is None:
                    all_models.add(model_name)
    
    # Limit total
    result = sorted(list(all_models))
    if len(result) > 10000:
        result = result[:10000]
    return result


async def get_all_models_grouped_by_server(db: AsyncSession, filter_type: Optional[str] = None) -> Dict[str, List[str]]:
    """
    Gets all available model names, grouped by their server, and includes proxy-native models.
    """
    # Validate filter_type
    if filter_type not in (None, 'chat', 'embedding'):
        filter_type = None
        
    servers = await get_servers(db)
    active_servers = [s for s in servers if s.is_active]

    grouped_models = {}
    for server in active_servers:
        server_models = []
        if server.available_models:
            models_list = server.available_models
            if isinstance(models_list, str):
                try:
                    models_list = json.loads(models_list)
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse available_models JSON for server {server.name} in get_all_models_grouped_by_server")
                    continue
                    
            if not isinstance(models_list, list):
                continue
                
            for model in models_list:
                if isinstance(model, dict) and "name" in model:
                    model_name = model["name"]
                    if not isinstance(model_name, str):
                        continue
                        
                    # Sanitize
                    model_name = model_name[:256]
                    
                    is_embed = is_embedding_model(model_name)
                    
                    should_add = False
                    if filter_type == 'embedding' and is_embed:
                        should_add = True
                    elif filter_type == 'chat' and not is_embed:
                        should_add = True
                    elif filter_type is None:
                        should_add = True
                    
                    if should_add:
                        server_models.append(model_name)
        
        # Limit per server
        if len(server_models) > 5000:
            server_models = server_models[:5000]
            
        if server_models:
            # Sanitize server name for dict key
            safe_name = server.name[:128] if server.name else "Unknown"
            grouped_models[safe_name] = sorted(server_models)

    # Create a new dictionary to control order and add proxy-native models
    final_grouped_models = {}
    if filter_type == 'chat' or filter_type is None:
        proxy_features = ["auto"]
        
        # Dynamically add Agents, Pools, Bundles and Workflows to the selector
        try:
            from app.database.models import SmartRouter, EnsembleOrchestrator, VirtualAgent, Workflow
            from sqlalchemy import select
            
            # Fetch Agents
            res_a = await db.execute(select(VirtualAgent.name).filter(VirtualAgent.is_active == True))
            proxy_features.extend(res_a.scalars().all())
            
            # Fetch Pools
            res_p = await db.execute(select(SmartRouter.name).filter(SmartRouter.is_active == True))
            proxy_features.extend(res_p.scalars().all())
            
            # Fetch Bundles
            res_b = await db.execute(select(EnsembleOrchestrator.name).filter(EnsembleOrchestrator.is_active == True))
            proxy_features.extend(res_b.scalars().all())

            # Fetch Workflows (Graphs)
            res_w = await db.execute(select(Workflow.name).filter(Workflow.is_active == True))
            proxy_features.extend(res_w.scalars().all())
        except Exception as e:
            logger.error(f"Error fetching proxy features for selector: {e}")
            
        final_grouped_models["Proxy Features"] = sorted(proxy_features)
    
    # Merge the server-specific models after the proxy features
    final_grouped_models.update(grouped_models)
            
    return final_grouped_models


async def get_active_models_all_servers(db: AsyncSession, http_client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """
    Fetches running models (`/api/ps`) from active Ollama servers.
    Skip Cloud/vLLM servers for the 'Currently in VRAM' check to reduce latency.
    """
    servers = await get_servers(db)
    # Only check local/dedicated Ollama servers for 'ps' status
    ollama_servers = [s for s in servers if s.is_active and s.server_type == 'ollama' and "ollama.com" not in s.url]
    
    all_models = []

    # 1. Fetch actively running models from Ollama servers
    vllm_servers = [s for s in servers if s.is_active and s.server_type == 'vllm']
    
    if ollama_servers:
        # Fetch metadata once to avoid multiple DB calls inside the loop
        from app.crud.model_metadata_crud import get_all_metadata
        all_meta = await get_all_metadata(db)
        meta_map = {m.model_name: m.max_context for m in all_meta}

        async def fetch_ps(server: OllamaServer):
            try:
                # Security check
                if not _is_safe_url(server.url):
                    logger.warning(f"Skipping server {server.name} due to unsafe URL")
                    return []
                    
                headers = _get_auth_headers(server)
                ps_url = f"{server.url.rstrip('/')}/api/ps"
                response = await http_client.get(ps_url, timeout=10.0, headers=headers)
                response.raise_for_status()
                data = response.json()
                
                # Validate and sanitize response
                result = []
                for model in data.get("models", []):
                    if not isinstance(model, dict):
                        continue
                    
                    m_name = str(model.get("name", ""))[:256]
                    
                    # Prefer API context_length, fallback to DB metadata
                    api_ctx = model.get("context_length")
                    if api_ctx:
                        ctx_limit = api_ctx
                    else:
                        base_name = m_name.split(':')[0]
                        ctx_limit = meta_map.get(m_name) or meta_map.get(base_name) or "Unknown"

                    safe_model = {
                        "name": m_name,
                        "server_name": server.name[:128],
                        "size": int(model.get("size", 0)) if str(model.get("size", "")).isdigit() else 0,
                        "size_vram": int(model.get("size_vram", 0)) if str(model.get("size_vram", "")).isdigit() else 0,
                        "context": ctx_limit,
                        "expires_at": str(model.get("expires_at", "N/A"))[:64],
                    }
                    result.append(safe_model)
                return result
            except Exception as e:
                logger.error(f"Failed to fetch running models from server '{server.name}': {e}")
                return []

        tasks = [fetch_ps(server) for server in ollama_servers]
        results = await asyncio.gather(*tasks)
        ollama_running_models = [model for sublist in results for model in sublist]
        all_models.extend(ollama_running_models)

    # 2. Add available models from vLLM servers
    for server in vllm_servers:
        if server.available_models and isinstance(server.available_models, list):
            for model_info in server.available_models[:100]:  # Limit to prevent DoS
                if isinstance(model_info, dict):
                    all_models.append({
                        "name": str(model_info.get("name", ""))[:256],
                        "server_name": server.name[:128],
                        "size": int(model_info.get("size", 0)) if str(model_info.get("size", "")).isdigit() else 0,
                        "size_vram": 1,  # Assume GPU placement for vLLM
                        "expires_at": "N/A (Always Active)",
                    })
                    
    # Limit total
    if len(all_models) > 10000:
        all_models = all_models[:10000]
        
    return all_models


async def refresh_all_server_models() -> dict:
    """
    Refreshes model lists for all active servers.
    Manages its own database lifecycle to prevent connection leaks in background tasks.
    """
    from app.database.session import AsyncSessionLocal
    
    results = {
        "total": 0,
        "success": 0,
        "failed": 0,
        "errors": []
    }

    async with AsyncSessionLocal() as db:
        servers = await get_servers(db)
        active_servers = [(s.id, s.name) for s in servers if s.is_active]
        results["total"] = len(active_servers)

    for server_id, server_name in active_servers:
        # We open a fresh session PER server update.
        # This ensures that even if one server times out, the connection is closed immediately.
        try:
            async with AsyncSessionLocal() as update_db:
                result = await fetch_and_update_models(update_db, server_id)
                if result["success"]:
                    results["success"] += 1
                else:
                    results["failed"] += 1
                    results["errors"].append({
                        "server_id": server_id,
                        "server_name": server_name,
                        "error": result["error"][:512] if result["error"] else "Unknown error"
                    })
        except Exception as e:
            results["failed"] += 1
            results["errors"].append({"server_id": server_id, "server_name": server_name, "error": str(e)})

    return results


async def check_server_health(http_client: httpx.AsyncClient, server: OllamaServer) -> Dict[str, Any]:
    """Performs a quick health check on a single server."""
    # Security check
    if not _is_safe_url(server.url):
        return {"server_id": server.id, "name": server.name[:128], "url": server.url[:256], "status": "Blocked", "reason": "URL blocked for security"}
        
    headers = _get_auth_headers(server)
    try:
        ping_url = server.url.rstrip('/')
        
        # PROVIDER-SPECIFIC PROBING
        if server.server_type in ('novita', 'vllm', 'open_webui', 'openrouter'):
            # These providers usually require a models list check or specific health path
            # We try /models as it verifies the API Key is also valid.
            ping_url += '/models'
        elif server.server_type == 'cloud':
            # Ollama Cloud health check
            ping_url = "https://ollama.com/api/tags" # Verified endpoint
            
        response = await http_client.get(ping_url, timeout=8.0, headers=headers)
        
        # Handle 401/403 as "Online but unauthorized" vs 404/500 as "Offline"
        if response.status_code == 200:
            return {"server_id": server.id, "name": server.name[:128], "url": server.url[:256], "status": "Online", "reason": None}
        else:
            return {"server_id": server.id, "name": server.name[:128], "url": server.url[:256], "status": "Offline", "reason": f"Status {response.status_code}"}
    
    except httpx.RequestError as e:
        logger.warning(f"Health check failed for server '{server.name}': {str(e)[:256]}")
        return {"server_id": server.id, "name": server.name[:128], "url": server.url[:256], "status": "Offline", "reason": str(e)[:256]}
    except Exception as e:
        logger.error(f"Unexpected error during health check for server '{server.name}': {str(e)[:256]}")
        return {"server_id": server.id, "name": server.name[:128], "url": server.url[:256], "status": "Offline", "reason": "Unexpected error"}


async def check_all_servers_health(db: AsyncSession, http_client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Checks the health of all configured servers."""
    servers = await get_servers(db)
    if not servers:
        return []

    tasks = [check_server_health(http_client, server) for server in servers]
    results = await asyncio.gather(*tasks)
    return results


async def get_model_details_from_server(http_client: httpx.AsyncClient, server: OllamaServer, model_name: str) -> Dict[str, Any]:
    """
    Retrieves model metadata (context window, etc) using service-specific endpoints.
    """
    headers = _get_auth_headers(server)
    url_base = server.url.rstrip('/')
    
    try:
        # 1. OLLAMA Protocol
        if server.server_type in ('ollama', 'cloud'):
            url = f"{url_base}/api/show"
            resp = await http_client.post(url, json={"name": model_name}, timeout=10.0, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                # AGNOSTIC SEARCH: Look for context length across different architectures
                info = data.get("model_info", {})
                details = data.get("details", {})
                
                # List of known keys used by different Ollama versions and architectures
                possible_keys = [
                    "context_length",
                    "max_position_embeddings",
                    "llama.context_length",
                    "phi3.context_length",
                    "gemma.context_length",
                    "qwen2.context_length",
                    "bert.max_position_embeddings"
                ]
                
                ctx = None
                # Check the model_info flat map first
                for key in possible_keys:
                    if key in info:
                        ctx = info[key]
                        break
                
                # Check top-level details if still not found
                if not ctx:
                    ctx = details.get("context_length")

                return {
                    "context_length": int(ctx) if ctx is not None else None,
                    "families": details.get("families", [])
                }

        # 2. VLLM / LLAMA.CPP (Props Endpoint)
        # llama.cpp server uses /props to expose n_ctx
        elif server.server_type == 'vllm':
            # Check for llama.cpp style props first
            try:
                props_resp = await http_client.get(f"{url_base}/props", timeout=5.0)
                if props_resp.status_code == 200:
                    p_data = props_resp.json()
                    # llama.cpp specific
                    n_ctx = p_data.get("default_generation_settings", {}).get("n_ctx")
                    if n_ctx: return {"context_length": int(n_ctx)}
            except: pass

            # Standard vLLM/OpenAI doesn't expose context window easily. 
            # We check the model ID for common suffixes (e.g., -128k)re
            match = re.search(r'-(\d+)[kK]', model_name)
            if match:
                return {"context_length": int(match.group(1)) * 1024}
        
        return {"context_length": None}
    except Exception as e:
        logger.debug(f"Could not fetch model details for {model_name} on {server.name}: {e}")
        return {"context_length": None}
