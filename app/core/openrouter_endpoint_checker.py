"""
OpenRouter Endpoint Checker
Checks if OpenRouter models have active endpoints using the OpenRouter API
"""
import logging
import httpx
from typing import Dict, Set, Optional
from app.core.encryption import decrypt_data

logger = logging.getLogger(__name__)


async def check_openrouter_model_endpoints(
    model_name: str,
    api_key: str,
    http_client: httpx.AsyncClient
) -> bool:
    """
    Check if an OpenRouter model has active endpoints.
    
    Args:
        model_name: Full model name (e.g., "openai/gpt-4o-mini")
        api_key: OpenRouter API key
        http_client: HTTP client for making requests
    
    Returns:
        True if model has at least one active endpoint, False otherwise
    """
    try:
        # Parse model name to get author and slug
        # Format: "author/slug" or "author/slug:tag"
        parts = model_name.split(":")
        base_name = parts[0]
        name_parts = base_name.split("/", 1)
        
        if len(name_parts) != 2:
            logger.warning(f"Invalid OpenRouter model name format: {model_name}")
            return False
        
        author, slug = name_parts
        
        # Call OpenRouter endpoints API
        url = f"https://openrouter.ai/api/v1/models/{author}/{slug}/endpoints"
        headers = {"Authorization": f"Bearer {api_key}"}
        
        response = await http_client.get(url, headers=headers, timeout=10.0)
        
        if response.status_code == 404:
            # Model doesn't exist or no endpoints
            logger.debug(f"Model {model_name} not found or has no endpoints (404)")
            return False
        
        response.raise_for_status()
        data = response.json()
        
        # According to proven API behavior: non-empty endpoints array = routable
        # Empty endpoints array or 404 = deprecated/unroutable
        # API structure: {"data": {"id": "...", "endpoints": [...]}}
        endpoints = data.get("data", {}).get("endpoints", [])
        if not endpoints or len(endpoints) == 0:
            logger.debug(f"Model {model_name} has no active endpoints (empty endpoints array)")
            return False
        
        # Non-empty endpoints array means the model is routable
        logger.debug(f"Model {model_name} has active endpoints ({len(endpoints)} endpoint(s))")
        return True
        
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.debug(f"Model {model_name} endpoints not found (404)")
            return False
        logger.warning(f"HTTP error checking endpoints for {model_name}: {e.response.status_code}")
        return False
    except Exception as e:
        logger.warning(f"Error checking endpoints for {model_name}: {e}")
        return False


async def filter_models_with_active_endpoints(
    model_names: list[str],
    server_api_key: str,
    http_client: httpx.AsyncClient
) -> Set[str]:
    """
    Filter a list of OpenRouter model names to only include those with active endpoints.
    
    Args:
        model_names: List of model names to check
        server_api_key: OpenRouter API key
        http_client: HTTP client for making requests
    
    Returns:
        Set of model names that have active endpoints
    """
    active_models = set()
    
    # Check models in parallel (with reasonable concurrency limit)
    import asyncio
    semaphore = asyncio.Semaphore(10)  # Limit to 10 concurrent requests
    
    async def check_with_semaphore(model_name: str):
        async with semaphore:
            is_active = await check_openrouter_model_endpoints(model_name, server_api_key, http_client)
            if is_active:
                active_models.add(model_name)
    
    tasks = [check_with_semaphore(name) for name in model_names]
    await asyncio.gather(*tasks, return_exceptions=True)
    
    logger.info(f"Filtered {len(model_names)} models: {len(active_models)} have active endpoints")
    return active_models

