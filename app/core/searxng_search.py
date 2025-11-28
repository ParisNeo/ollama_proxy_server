"""
SearXNG Web Search Service
Uses local SearXNG instance for web search
"""
import logging
import httpx
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class SearXNGSearchService:
    """Service for SearXNG web search"""
    
    def __init__(self, base_url: str = "http://localhost:7019", timeout: float = 20.0):
        """
        Initialize SearXNG search service
        
        Args:
            base_url: Base URL of SearXNG instance (default: http://localhost:7019)
            timeout: Request timeout in seconds (default: 20.0)
        """
        self.base_url = base_url.rstrip('/')
        # For localhost HTTPS, disable SSL verification (self-signed certs from Caddy)
        # For all other connections, keep SSL verification enabled
        is_localhost = "localhost" in base_url or "127.0.0.1" in base_url
        verify_ssl = not (is_localhost and base_url.startswith("https://"))
        self.client = httpx.AsyncClient(
            timeout=timeout, 
            follow_redirects=True,
            verify=verify_ssl
        )
    
    async def web_search(
        self, 
        query: str, 
        max_results: int = 5
    ) -> Dict[str, Any]:
        """
        Perform web search using SearXNG
        
        Args:
            query: Search query string
            max_results: Maximum results to return (default 5)
            
        Returns:
            Dict with 'results' array containing search results in Ollama-compatible format
        """
        if max_results > 20:
            max_results = 20
        if max_results < 1:
            max_results = 5
        
        try:
            # SearXNG API: /search?q=query&format=json
            # Don't specify engines - let SearXNG use its default configured engines
            # Specifying engines can cause 400 errors if those engines aren't available
            
            # Try the configured URL first
            try:
                response = await self.client.get(
                    f"{self.base_url}/search",
                    params={
                        "q": query,
                        "format": "json"
                    }
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                # If we get "Client sent an HTTP request to an HTTPS server" error,
                # the server is actually HTTPS but we're using HTTP
                if e.response.status_code == 400 and "HTTPS" in str(e.response.text):
                    # Server requires HTTPS - retry with HTTPS
                    https_url = self.base_url.replace("http://", "https://")
                    logger.info(f"SearXNG requires HTTPS, retrying with {https_url}")
                    # For localhost HTTPS, create a client with SSL verification disabled
                    is_localhost = "localhost" in https_url or "127.0.0.1" in https_url
                    verify_ssl = not is_localhost
                    https_client = httpx.AsyncClient(
                        timeout=self.client.timeout,
                        follow_redirects=True,
                        verify=verify_ssl
                    )
                    try:
                        response = await https_client.get(
                            f"{https_url}/search",
                            params={
                                "q": query,
                                "format": "json"
                            }
                        )
                        response.raise_for_status()
                    finally:
                        await https_client.aclose()
                else:
                    raise
            
            searxng_data = response.json()
            
            # Convert SearXNG format to Ollama-compatible format
            results = []
            if "results" in searxng_data:
                for item in searxng_data["results"][:max_results]:
                    results.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "content": item.get("content", "") or item.get("snippet", ""),
                        "engine": "searxng"
                    })
            
            return {
                "results": results,
                "query": query,
                "engine": "searxng"
            }
        except httpx.TimeoutException:
            logger.warning(f"SearXNG search timeout for query: {query}")
            raise
        except httpx.HTTPStatusError as e:
            error_detail = ""
            try:
                error_detail = e.response.text[:500]  # First 500 chars of error
            except:
                pass
            logger.error(
                f"SearXNG search HTTP error {e.response.status_code} for URL: {e.request.url}\n"
                f"Error detail: {error_detail}"
            )
            raise
        except Exception as e:
            logger.error(f"SearXNG search error: {e}", exc_info=True)
            raise
    
    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose()

