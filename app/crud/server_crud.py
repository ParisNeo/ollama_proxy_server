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

logger = logging.getLogger(__name__)

def _get_auth_headers(server: OllamaServer) -> Dict[str, str]:
    headers = {}
    if server.encrypted_api_key:
        api_key = decrypt_data(server.encrypted_api_key)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    return headers

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
        query = query.limit(limit)
    result = await db.execute(query)
    return result.scalars().all()

async def create_server(db: AsyncSession, server: ServerCreate) -> OllamaServer:
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
            # FIX: Convert Pydantic URL object to a string before setting.
            if key == 'url':
                setattr(db_server, key, str(value))
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
    
    headers = _get_auth_headers(server)

    try:
        models = []
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            if server.server_type == "vllm":
                endpoint_url = f"{server.url.rstrip('/')}/v1/models"
                response = await client.get(endpoint_url)
                response.raise_for_status()
                data = response.json()
                models_data = data.get("data", [])
                for model in models_data:
                    model_id = model.get("id")
                    if not model_id: continue
                    
                    family = model_id.split(':')[0].split('-')[0] # Best guess for family

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
                models = data.get("models", [])
        
        server.available_models = models
        server.models_last_updated = datetime.datetime.utcnow()
        server.last_error = None
        await db.commit()
        await db.refresh(server)

        logger.info(f"Successfully fetched {len(models)} models from {server.server_type} server '{server.name}'")
        return {"success": True, "models": models, "error": None}

    except httpx.HTTPError as e:
        error_msg = f"HTTP error: {str(e)}"
        logger.error(f"Failed to fetch models from server '{server.name}': {error_msg}")
        server.last_error = error_msg
        server.available_models = None
        await db.commit()
        return {"success": False, "error": error_msg, "models": []}
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(f"Failed to fetch models from server '{server.name}': {error_msg}")
        server.last_error = error_msg
        server.available_models = None
        await db.commit()
        return {"success": False, "error": error_msg, "models": []}


async def pull_model_on_server(http_client: httpx.AsyncClient, server: OllamaServer, model_name: str) -> dict:
    """Pulls a model on a specific Ollama server."""
    if server.server_type == 'vllm':
        return {"success": False, "message": "Pulling models is not supported for vLLM servers."}
        
    headers = _get_auth_headers(server)
    pull_url = f"{server.url.rstrip('/')}/api/pull"
    payload = {"name": model_name, "stream": False}
    try:
        # Use a long timeout as pulling can take a significant amount of time
        async with http_client.stream("POST", pull_url, json=payload, timeout=1800.0, headers=headers) as response:
            async for chunk in response.aiter_text():
                try:
                    line = json.loads(chunk)
                    # You could process status updates here if needed in the future
                    logger.debug(f"Pull status for {model_name} on {server.name}: {line.get('status')}")
                except json.JSONDecodeError:
                    continue # Ignore non-json chunks
        
        response.raise_for_status() # Will raise an exception for 4xx/5xx responses
        logger.info(f"Successfully pulled/updated model '{model_name}' on server '{server.name}'")
        return {"success": True, "message": f"Model '{model_name}' pulled/updated successfully."}
    except httpx.HTTPStatusError as e:
        error_msg = f"Failed to pull model '{model_name}': Server returned status {e.response.status_code}"
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}
    except Exception as e:
        error_msg = f"An unexpected error occurred while pulling model '{model_name}': {e}"
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}

async def delete_model_on_server(http_client: httpx.AsyncClient, server: OllamaServer, model_name: str) -> dict:
    """Deletes a model from a specific Ollama server."""
    if server.server_type == 'vllm':
        return {"success": False, "message": "Deleting models is not supported for vLLM servers."}

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
        error_msg = f"Failed to delete model '{model_name}': Server returned status {e.response.status_code}"
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}
    except Exception as e:
        error_msg = f"An unexpected error occurred while deleting model '{model_name}': {e}"
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}

async def load_model_on_server(http_client: httpx.AsyncClient, server: OllamaServer, model_name: str) -> dict:
    """Sends a dummy request to a server to load a model into memory."""
    if server.server_type == 'vllm':
        return {"success": False, "message": "Explicit model loading is not applicable for vLLM servers."}

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
        error_msg = f"Failed to load model '{model_name}': Server returned status {e.response.status_code}: {error_detail}"
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}
    except Exception as e:
        error_msg = f"An unexpected error occurred while loading model '{model_name}': {e}"
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}

async def unload_model_on_server(http_client: httpx.AsyncClient, server: OllamaServer, model_name: str) -> dict:
    """Sends a request to a server to unload a model from memory."""
    if server.server_type == 'vllm':
        return {"success": False, "message": "Explicit model unloading is not applicable for vLLM servers."}

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
        error_msg = f"Failed to unload model '{model_name}': Server returned status {e.response.status_code}: {error_detail}"
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}
    except Exception as e:
        error_msg = f"An unexpected error occurred while unloading model '{model_name}': {e}"
        logger.error(f"{error_msg} on server '{server.name}'")
        return {"success": False, "message": error_msg}

async def get_servers_with_model(db: AsyncSession, model_name: str) -> list[OllamaServer]:
    """
    Get all active servers that have the specified model available, using flexible matching.
    """
    servers = await get_servers(db)
    active_servers = [s for s in servers if s.is_active]

    servers_with_model = []
    for server in active_servers:
        if server.available_models:
            for model_data in server.available_models:
                if isinstance(model_data, dict) and "name" in model_data:
                    available_model_name = model_data["name"]
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
    return "embed" in model_name.lower()

async def get_all_available_model_names(db: AsyncSession, filter_type: Optional[str] = None) -> List[str]:
    """
    Gets a unique, sorted list of all model names across all active servers.
    Can be filtered by type ('chat' or 'embedding').
    """
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

        for model in models_list:
            if isinstance(model, dict) and "name" in model:
                model_name = model["name"]
                is_embed = is_embedding_model(model_name)
                
                if filter_type == 'embedding' and is_embed:
                    all_models.add(model_name)
                elif filter_type == 'chat' and not is_embed:
                    all_models.add(model_name)
                elif filter_type is None:
                    all_models.add(model_name)
    
    return sorted(list(all_models))

async def get_all_models_grouped_by_server(db: AsyncSession, filter_type: Optional[str] = None) -> Dict[str, List[str]]:
    """
    Gets all available model names, grouped by their server, and includes proxy-native models.
    """
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
                    
            for model in models_list:
                if isinstance(model, dict) and "name" in model:
                    model_name = model["name"]
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
        
        if server_models:
            grouped_models[server.name] = sorted(server_models)

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
                headers = _get_auth_headers(server)
                ps_url = f"{server.url.rstrip('/')}/api/ps"
                response = await http_client.get(ps_url, timeout=5.0, headers=headers)
                response.raise_for_status()
                data = response.json()
                # Add server info to each running model
                for model in data.get("models", []):
                    model["server_name"] = server.name
                return data.get("models", [])
            except Exception as e:
                logger.error(f"Failed to fetch running models from server '{server.name}': {e}")
                return []

        tasks = [fetch_ps(server) for server in ollama_servers]
        results = await asyncio.gather(*tasks)
        ollama_running_models = [model for sublist in results for model in sublist]
        all_models.extend(ollama_running_models)

    # 2. Add available models from vLLM servers
    for server in vllm_servers:
        if server.available_models:
            for model_info in server.available_models:
                all_models.append({
                    "name": model_info.get("name"),
                    "server_name": server.name,
                    "size": model_info.get("size", 0),
                    "size_vram": 1,  # Assume GPU placement for vLLM
                    "expires_at": "N/A (Always Active)",
                })
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
                "error": result["error"]
            })

    return results

async def check_server_health(http_client: httpx.AsyncClient, server: OllamaServer) -> Dict[str, Any]:
    """Performs a quick health check on a single Ollama server."""
    headers = _get_auth_headers(server)
    try:
        ping_url = server.url.rstrip('/')
        # vLLM servers have a /health endpoint, Ollama root is enough
        if server.server_type == 'vllm':
            ping_url += '/health'
            
        response = await http_client.get(ping_url, timeout=3.0, headers=headers)
        
        if response.status_code == 200:
            return {"server_id": server.id, "name": server.name, "url": server.url, "status": "Online", "reason": None}
        else:
            return {"server_id": server.id, "name": server.name, "url": server.url, "status": "Offline", "reason": f"Status {response.status_code}"}
    
    except httpx.RequestError as e:
        logger.warning(f"Health check failed for server '{server.name}': {e}")
        return {"server_id": server.id, "name": server.name, "url": server.url, "status": "Offline", "reason": str(e)}
    except Exception as e:
        logger.error(f"Unexpected error during health check for server '{server.name}': {e}")
        return {"server_id": server.id, "name": server.name, "url": server.url, "status": "Offline", "reason": "Unexpected error"}

async def check_all_servers_health(db: AsyncSession, http_client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Checks the health of all configured servers."""
    servers = await get_servers(db)
    if not servers:
        return []

    tasks = [check_server_health(http_client, server) for server in servers]
    results = await asyncio.gather(*tasks)
    return results
