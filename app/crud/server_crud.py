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
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _get_auth_headers(server: OllamaServer) -> Dict[str, str]:
    headers = {}
    if server.encrypted_api_key:
        api_key = decrypt_data(server.encrypted_api_key)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _is_safe_url(url: str) -> bool:
    """
    Validates that the provided URL is well-formed and uses a supported scheme.
    SSRF protection relies on the fact that only authenticated admins can add servers.
    Localhost and private IPs are explicitly allowed for local infrastructure.
    """
    try:
        parsed = urlparse(str(url))
        # Ensure only http/https schemes are used
        if parsed.scheme not in ('http', 'https'):
            return False
            
        # Ensure there is a valid hostname/netloc
        if not parsed.netloc:
            return False
            
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
    # Validate URL safety
    if not _is_safe_url(str(server.url)):
        raise ValueError(f"URL {server.url} is not allowed for security reasons. Internal IPs and localhost are blocked.")
        
    # Validate name
    if not server.name or len(server.name) > 128:
        raise ValueError("Server name must be 1-128 characters")
        
    # Validate server_type
    if server.server_type not in ('ollama', 'vllm'):
        raise ValueError("Server type must be 'ollama' or 'vllm'")

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
    
    if "api_key" in update_data:
        api_key = update_data.pop("api_key")
        # A non-None value in api_key means we are intentionally setting/updating/clearing it.
        # An empty string will clear it.
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
                if value not in ('ollama', 'vllm'):
                    raise ValueError("Server type must be 'ollama' or 'vllm'")
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
            if server.server_type == "vllm":
                endpoint_url = f"{server.url.rstrip('/')}/v1/models"
                response = await client.get(endpoint_url)
                response.raise_for_status()
                data = response.json()
                models_data = data.get("data", [])
                
                # Validate model data structure to prevent injection
                for model in models_data:
                    if not isinstance(model, dict):
                        continue
                    model_id = model.get("id")
                    if not model_id or not isinstance(model_id, str):
                        continue
                    # Sanitize model_id
                    model_id = re.sub(r'[^\w\.\-:/]', '', model_id)[:256]
                    
                    family = model_id.split(':')[0].split('-')[0] if ':' in model_id else model_id.split('-')[0]
                    family = re.sub(r'[^\w\.\-]', '', family)[:64]

                    models.append({
                        "name": model_id,
                        "size": 0,  # Not available from vLLM API
                        "modified_at": datetime.datetime.fromtimestamp(
                            model.get("created", 0), tz=datetime.timezone.utc
                        ).isoformat(),
                        "digest": model_id, # Use ID as a stand-in for digest
                        "details": {
                            "parent_model": "",
                            "format": "vllm",
                            "family": family,
                            "families": [family] if family else None,
                            "parameter_size": "N/A",
                            "quantization_level": "N/A"
                        }
                    })
            else:  # Default to "ollama"
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
        # Use a long timeout as pulling can take a significant amount of time
        # But cap it to prevent indefinite hanging
        async with http_client.stream("POST", pull_url, json=payload, timeout=1800.0, headers=headers) as response:
            # Check for immediate errors
            if response.status_code >= 400:
                error_text = await response.aread()
                raise httpx.HTTPStatusError(
                    f"Pull failed with status {response.status_code}", 
                    request=response.request, 
                    response=response
                )
                
            async for chunk in response.aiter_text():
                try:
                    line = json.loads(chunk)
                    # You could process status updates here if needed in the future
                    logger.debug(f"Pull status for {model_name} on {server.name}: {line.get('status')}")
                except json.JSONDecodeError:
                    continue # Ignore non-json chunks
        
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
    Get all active servers that have the specified model available, using flexible matching.
    """
    # Validate model name
    if not model_name or len(model_name) > 256:
        return []
        
    # Sanitize model name for safety
    model_name = re.sub(r'[^\w\.\-:@]', '', model_name)[:256]

    servers = await get_servers(db)
    active_servers = [s for s in servers if s.is_active]

    servers_with_model = []
    for server in active_servers:
        if server.available_models:
            # Validate available_models is a list
            models_list = server.available_models
            if isinstance(models_list, str):
                try:
                    models_list = json.loads(models_list)
                except json.JSONDecodeError:
                    continue
            
            if not isinstance(models_list, list):
                continue
                
            for model_data in models_list:
                if isinstance(model_data, dict) and "name" in model_data:
                    available_model_name = model_data["name"]
                    if not isinstance(available_model_name, str):
                        continue
                        
                    # Flexible matching:
                    # 1. Exact match (e.g., "llama3:8b" == "llama3:8b")
                    # 2. Prefix match (e.g., "llama3" matches "llama3:8b")
                    # 3. Substring match for vLLM (e.g., "Llama-2-7b" matches "models--meta-llama--Llama-2-7b-chat-hf")
                    if (available_model_name == model_name or 
                        available_model_name.startswith(f"{model_name}:") or
                        (server.server_type == 'vllm' and model_name in available_model_name)):
                        servers_with_model.append(server)
                        break  # Found on this server, move to the next
    return servers_with_model


def is_embedding_model(model_name: str) -> bool:
    """Heuristically determines if a model is for embeddings."""
    if not isinstance(model_name, str):
        return False
    return "embed" in model_name.lower()


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

    # Create a new dictionary to control order and add proxy-native models like 'auto'
    final_grouped_models = {}
    if filter_type == 'chat' or filter_type is None:
        final_grouped_models["Proxy Features"] = ["auto"]
    
    # Merge the server-specific models after the proxy features
    final_grouped_models.update(grouped_models)
            
    return final_grouped_models


async def get_active_models_all_servers(db: AsyncSession, http_client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """
    Fetches running models (`/api/ps`) from active Ollama servers and
    lists available models from active vLLM servers as they are always 'active'.
    """
    servers = await get_servers(db)
    active_servers = [s for s in servers if s.is_active]
    
    ollama_servers = [s for s in active_servers if s.server_type == 'ollama']
    vllm_servers = [s for s in active_servers if s.server_type == 'vllm']
    
    all_models = []

    # 1. Fetch actively running models from Ollama servers
    if ollama_servers:
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
                    safe_model = {
                        "name": str(model.get("name", ""))[:256],
                        "server_name": server.name[:128],
                        "size": int(model.get("size", 0)) if str(model.get("size", "")).isdigit() else 0,
                        "size_vram": int(model.get("size_vram", 0)) if str(model.get("size_vram", "")).isdigit() else 0,
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


async def refresh_all_server_models(db: AsyncSession) -> dict:
    """
    Refreshes model lists for all active servers.

    Returns:
        dict with 'total', 'success', 'failed' counts
    """
    # Get all servers and extract their IDs/names before any async operations
    servers = await get_servers(db)
    active_servers = [(s.id, s.name, s.is_active) for s in servers]
    active_servers = [(sid, sname) for sid, sname, is_active in active_servers if is_active]

    results = {
        "total": len(active_servers),
        "success": 0,
        "failed": 0,
        "errors": []
    }

    for server_id, server_name in active_servers:
        result = await fetch_and_update_models(db, server_id)
        if result["success"]:
            results["success"] += 1
        else:
            results["failed"] += 1
            results["errors"].append({
                "server_id": server_id,
                "server_name": server_name,
                "error": result["error"][:512] if result["error"] else "Unknown error"
            })

    return results


async def check_server_health(http_client: httpx.AsyncClient, server: OllamaServer) -> Dict[str, Any]:
    """Performs a quick health check on a single Ollama server."""
    # Security check
    if not _is_safe_url(server.url):
        return {"server_id": server.id, "name": server.name[:128], "url": server.url[:256], "status": "Blocked", "reason": "URL blocked for security"}
        
    headers = _get_auth_headers(server)
    try:
        ping_url = server.url.rstrip('/')
        # vLLM servers have a /health endpoint, Ollama root is enough
        if server.server_type == 'vllm':
            ping_url += '/health'
            
        response = await http_client.get(ping_url, timeout=5.0, headers=headers)
        
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
