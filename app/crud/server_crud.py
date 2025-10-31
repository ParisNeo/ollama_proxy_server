from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models import OllamaServer
from app.schema.server import ServerCreate
import httpx
import logging
import datetime
from typing import Optional, List, Dict, Any
import asyncio

logger = logging.getLogger(__name__)

async def get_server_by_url(db: AsyncSession, url: str) -> OllamaServer | None:
    result = await db.execute(select(OllamaServer).filter(OllamaServer.url == url))
    return result.scalars().first()

async def get_servers(db: AsyncSession, skip: int = 0, limit: Optional[int] = None) -> list[OllamaServer]:
    query = select(OllamaServer).order_by(OllamaServer.created_at.desc()).offset(skip)
    if limit is not None:
        query = query.limit(limit)
    result = await db.execute(query)
    return result.scalars().all()

async def create_server(db: AsyncSession, server: ServerCreate) -> OllamaServer:
    db_server = OllamaServer(name=server.name, url=str(server.url))
    db.add(db_server)
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
    Fetches the list of available models from an Ollama server's /api/tags endpoint
    and updates the database with the results.

    Returns a dict with 'success' (bool), 'models' (list), and optionally 'error' (str)
    """
    result = await db.execute(select(OllamaServer).filter(OllamaServer.id == server_id))
    server = result.scalars().first()

    if not server:
        return {"success": False, "error": "Server not found", "models": []}

    try:
        # Construct the tags endpoint URL
        tags_url = f"{server.url.rstrip('/')}/api/tags"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(tags_url)
            response.raise_for_status()
            data = response.json()

            # Extract models from the response
            models = data.get("models", [])

            # Update the server record with full model data
            server.available_models = models
            server.models_last_updated = datetime.datetime.utcnow()
            await db.commit()
            await db.refresh(server)

            logger.info(f"Successfully fetched {len(models)} models from server '{server.name}' ({server.url})")
            return {"success": True, "models": models, "error": None}

    except httpx.HTTPError as e:
        error_msg = f"HTTP error: {str(e)}"
        logger.error(f"Failed to fetch models from server '{server.name}' ({server.url}): {error_msg}")
        return {"success": False, "error": error_msg, "models": []}
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(f"Failed to fetch models from server '{server.name}' ({server.url}): {error_msg}")
        return {"success": False, "error": error_msg, "models": []}

def get_model_names_from_server(server: OllamaServer) -> set[str]:
    """
    Extract all model names from a server's available_models field.
    Returns a set of model names for fast lookup.
    """
    if not server.available_models:
        return set()

    model_names = set()
    for model in server.available_models:
        if isinstance(model, dict) and "name" in model:
            # Add both the full name and any base name (without tags)
            full_name = model["name"]
            model_names.add(full_name)

            # Also add the base name without version tag (e.g., "llama2" from "llama2:latest")
            if ":" in full_name:
                base_name = full_name.split(":")[0]
                model_names.add(base_name)

    return model_names

async def get_servers_with_model(db: AsyncSession, model_name: str) -> list[OllamaServer]:
    """
    Get all active servers that have the specified model available.

    Args:
        db: Database session
        model_name: Name of the model to search for (can be with or without tag)

    Returns:
        List of OllamaServer instances that have the model
    """
    servers = await get_servers(db)
    active_servers = [s for s in servers if s.is_active]

    servers_with_model = []
    for server in active_servers:
        available_models = get_model_names_from_server(server)
        if model_name in available_models:
            servers_with_model.append(server)

    return servers_with_model

async def get_ollama_ps_all_servers(db: AsyncSession, http_client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """
    Fetches running models (`/api/ps`) from all active Ollama servers.
    """
    active_servers = [s for s in await get_servers(db) if s.is_active]
    if not active_servers:
        return []

    async def fetch_ps(server: OllamaServer):
        try:
            ps_url = f"{server.url.rstrip('/')}/api/ps"
            response = await http_client.get(ps_url, timeout=5.0)
            response.raise_for_status()
            data = response.json()
            # Add server info to each running model
            for model in data.get("models", []):
                model["server_name"] = server.name
            return data.get("models", [])
        except Exception as e:
            logger.error(f"Failed to fetch running models from server '{server.name}': {e}")
            return []

    tasks = [fetch_ps(server) for server in active_servers]
    results = await asyncio.gather(*tasks)
    
    # Flatten the list of lists
    all_running_models = [model for sublist in results for model in sublist]
    return all_running_models

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
    try:
        ping_url = server.url.rstrip('/')
        response = await http_client.get(ping_url, timeout=3.0)
        
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