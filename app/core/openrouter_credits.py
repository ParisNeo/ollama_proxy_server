"""
OpenRouter Credits and Account Information
Fetches account balance and credits from OpenRouter API
"""
import logging
import httpx
import json
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from app.core.openrouter_translator import OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)

async def get_openrouter_credits(api_key: str) -> Optional[Dict[str, Any]]:
    """
    Fetch OpenRouter account credits/balance information from /api/v1/credits
    
    Args:
        api_key: OpenRouter API key
        
    Returns:
        Dict with credits information (total_credits, total_usage, remaining_credits, refreshed_at) or None if failed
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Follow the exact example from API docs: GET with only Authorization header
            response = await client.get(
                f"{OPENROUTER_BASE_URL}/credits",
                headers={
                    "Authorization": f"Bearer {api_key}"
                }
            )
            
            # Log response status and body for debugging
            logger.info(f"OpenRouter credits API response status: {response.status_code}")
            response_text = response.text
            logger.info(f"OpenRouter credits API raw response body: {response_text[:500]}")
            
            # Check for 403 BEFORE raise_for_status (403 means API key doesn't have permission)
            if response.status_code == 403:
                logger.error(f"OpenRouter credits forbidden (403) - API key may not have permission to fetch credits. Only provisioning keys can access this endpoint. Response: {response_text[:500]}")
                return None
            
            # Check for other errors
            if response.status_code != 200:
                logger.error(f"OpenRouter credits API returned non-200 status: {response.status_code}. Response: {response_text[:500]}")
                response.raise_for_status()
            
            data = response.json()
            
            logger.info(f"OpenRouter credits API parsed JSON: {data}")
            
            # OpenRouter returns: {"data": {"total_credits": ..., "total_usage": ...}}
            # According to API docs: response has "data" object with "total_credits" and "total_usage"
            if "data" not in data:
                logger.error(f"OpenRouter credits response missing 'data' field. Full response: {data}")
                # Try direct access in case structure is different
                if "total_credits" in data:
                    logger.warning("Using direct data access (no 'data' wrapper) - unexpected structure")
                    data_dict = data
                else:
                    logger.error(f"Could not find credits data in response. Response keys: {list(data.keys())}")
                    logger.error(f"Full response: {json.dumps(data, indent=2)}")
                    return None
            else:
                data_dict = data["data"]
                if not isinstance(data_dict, dict):
                    logger.error(f"OpenRouter credits 'data' field is not a dict: {type(data_dict)}, value: {data_dict}")
                    return None
            
            # Extract values - these are required fields according to API docs
            if "total_credits" not in data_dict:
                logger.error(f"OpenRouter credits response missing 'total_credits' in data. Data keys: {list(data_dict.keys())}")
                logger.error(f"Full data dict: {json.dumps(data_dict, indent=2)}")
                return None
            if "total_usage" not in data_dict:
                logger.error(f"OpenRouter credits response missing 'total_usage' in data. Data keys: {list(data_dict.keys())}")
                logger.error(f"Full data dict: {json.dumps(data_dict, indent=2)}")
                return None
            
            total_credits = float(data_dict["total_credits"])
            total_usage = float(data_dict["total_usage"])
            remaining_credits = total_credits - total_usage
            
            logger.info(f"OpenRouter credits parsed - Total: {total_credits}, Used: {total_usage}, Remaining: {remaining_credits}")
            
            if total_credits == 0 and total_usage == 0:
                logger.warning(f"OpenRouter credits are both 0 - this may indicate the API key doesn't have access or account has no credits")
            
            return {
                "total_credits": total_credits,
                "total_usage": total_usage,
                "remaining_credits": remaining_credits,
                "refreshed_at": datetime.now(timezone.utc).isoformat()
            }
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenRouter credits HTTP error: {e.response.status_code}")
        logger.error(f"Response text: {e.response.text[:500]}")
        if e.response.status_code == 404:
            logger.debug(f"OpenRouter credits endpoint not available (404) - this may be expected")
        elif e.response.status_code == 403:
            logger.error(f"OpenRouter credits endpoint forbidden (403) - only provisioning keys can fetch credits. Full response: {e.response.text[:500]}")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch OpenRouter credits: {e}", exc_info=True)
        return None

