"""
Ollama Web Search and Fetch Service
Uses Ollama's native web search and fetch APIs
Supports multiple API keys with automatic rotation/fallback
"""
import os
import logging
import httpx
import random
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class OllamaWebSearchService:
    """Service for Ollama's web search and fetch APIs with multi-key support"""
    
    def __init__(self, api_key: Optional[str] = None, api_key_2: Optional[str] = None):
        """
        Initialize Ollama web search service
        
        Args:
            api_key: Primary Ollama API key. If None, reads from OLLAMA_API_KEY env var
            api_key_2: Secondary Ollama API key for load balancing/fallback
        """
        self.api_keys = []
        if api_key:
            self.api_keys.append(api_key)
        elif os.getenv("OLLAMA_API_KEY"):
            self.api_keys.append(os.getenv("OLLAMA_API_KEY"))
        
        if api_key_2:
            self.api_keys.append(api_key_2)
        elif os.getenv("OLLAMA_API_KEY_2"):
            self.api_keys.append(os.getenv("OLLAMA_API_KEY_2"))
        
        if not self.api_keys:
            self.api_key = None
        else:
            # Use first key as primary, rotate for load balancing
            self.api_key = self.api_keys[0]
        
        self.base_url = "https://ollama.com/api"
        self.client = httpx.AsyncClient(timeout=20.0)
        self._key_index = 0  # For rotation
        
    def _get_api_key(self) -> Optional[str]:
        """Get an API key with rotation/fallback"""
        if not self.api_keys:
            return None
        
        # Rotate keys for load balancing
        if len(self.api_keys) > 1:
            key = self.api_keys[self._key_index % len(self.api_keys)]
            self._key_index = (self._key_index + 1) % len(self.api_keys)
            return key
        
        return self.api_keys[0]
    
    async def web_search(
        self, 
        query: str, 
        max_results: int = 5,
        retry_with_fallback: bool = True
    ) -> Dict[str, Any]:
        """
        Perform web search using Ollama's API with automatic key rotation/fallback
        
        Args:
            query: Search query string
            max_results: Maximum results to return (default 5, max 10)
            retry_with_fallback: If True, try other keys on failure
            
        Returns:
            Dict with 'results' array containing search results
        """
        if not self.api_keys:
            raise ValueError("At least one OLLAMA_API_KEY is required. Set it in settings or environment.")
        
        if max_results > 10:
            max_results = 10
        if max_results < 1:
            max_results = 5
        
        # Try each key until one works
        keys_to_try = self.api_keys.copy()
        if not retry_with_fallback:
            keys_to_try = [self._get_api_key()]
        
        last_error = None
        for api_key in keys_to_try:
            try:
                response = await self.client.post(
                    f"{self.base_url}/web_search",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "query": query,
                        "max_results": max_results
                    }
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code == 401:
                    # Auth error - try next key
                    logger.warning(f"API key failed with 401, trying next key...")
                    continue
                else:
                    # Other HTTP error - don't retry
                    logger.error(f"Ollama web search HTTP error: {e.response.status_code} - {e.response.text}")
                    raise
            except Exception as e:
                last_error = e
                logger.error(f"Ollama web search error with key: {e}", exc_info=True)
                if retry_with_fallback and len(keys_to_try) > 1:
                    continue
                raise
        
        # All keys failed
        if last_error:
            raise last_error
        raise ValueError("No valid API keys available")
    
    async def web_fetch(self, url: str, retry_with_fallback: bool = True) -> Dict[str, Any]:
        """
        Fetch a web page using Ollama's API with automatic key rotation/fallback
        
        Args:
            url: URL to fetch
            retry_with_fallback: If True, try other keys on failure
            
        Returns:
            Dict with 'title', 'content', and 'links'
        """
        if not self.api_keys:
            raise ValueError("At least one OLLAMA_API_KEY is required. Set it in settings or environment.")
        
        # Try each key until one works
        keys_to_try = self.api_keys.copy()
        if not retry_with_fallback:
            keys_to_try = [self._get_api_key()]
        
        last_error = None
        for api_key in keys_to_try:
            try:
                response = await self.client.post(
                    f"{self.base_url}/web_fetch",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "url": url
                    }
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code == 401:
                    # Auth error - try next key
                    logger.warning(f"API key failed with 401, trying next key...")
                    continue
                else:
                    # Other HTTP error - don't retry
                    logger.error(f"Ollama web fetch HTTP error: {e.response.status_code} - {e.response.text}")
                    raise
            except Exception as e:
                last_error = e
                logger.error(f"Ollama web fetch error with key: {e}", exc_info=True)
                if retry_with_fallback and len(keys_to_try) > 1:
                    continue
                raise
        
        # All keys failed
        if last_error:
            raise last_error
        raise ValueError("No valid API keys available")
    
    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose()

