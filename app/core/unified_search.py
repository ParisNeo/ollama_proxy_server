"""
Unified Web Search Service
Tries SearXNG first, falls back to Ollama cloud service
"""
import logging
from typing import Dict, Any, Optional
from app.core.searxng_search import SearXNGSearchService
from app.core.ollama_web_search import OllamaWebSearchService

logger = logging.getLogger(__name__)

class UnifiedSearchService:
    """Unified search service that tries SearXNG first, then Ollama"""
    
    def __init__(
        self, 
        searxng_url: str = "http://localhost:7019",
        ollama_api_key: Optional[str] = None,
        ollama_api_key_2: Optional[str] = None,
        timeout: float = 20.0
    ):
        """
        Initialize unified search service
        
        Args:
            searxng_url: Base URL of SearXNG instance
            ollama_api_key: Primary Ollama API key for fallback
            ollama_api_key_2: Secondary Ollama API key for fallback
            timeout: Request timeout in seconds (default: 20.0)
        """
        self.searxng_service = SearXNGSearchService(base_url=searxng_url, timeout=timeout)
        self.ollama_service = OllamaWebSearchService(
            api_key=ollama_api_key,
            api_key_2=ollama_api_key_2
        ) if ollama_api_key or ollama_api_key_2 else None
        self.timeout = timeout
    
    async def web_search(
        self,
        query: str,
        max_results: int = 5,
        engine: Optional[str] = None  # "searxng", "ollama", or None for auto
    ) -> Dict[str, Any]:
        """
        Perform web search with automatic fallback
        
        Args:
            query: Search query string
            max_results: Maximum results to return
            engine: Force specific engine ("searxng" or "ollama"), or None for auto
            
        Returns:
            Dict with 'results' array and 'engine' field indicating which was used
        """
        # If engine is specified, use only that engine
        if engine == "searxng":
            try:
                return await self.searxng_service.web_search(query, max_results)
            except Exception as e:
                logger.error(f"SearXNG search failed: {e}")
                raise
        
        if engine == "ollama":
            if not self.ollama_service:
                raise ValueError("Ollama API key not configured")
            try:
                return await self.ollama_service.web_search(query, max_results)
            except Exception as e:
                logger.error(f"Ollama search failed: {e}")
                raise
        
        # Auto mode: Try SearXNG first, fallback to Ollama
        try:
            logger.info(f"Trying SearXNG search for query: {query}")
            result = await self.searxng_service.web_search(query, max_results)
            logger.info(f"SearXNG search succeeded, returned {len(result.get('results', []))} results")
            return result
        except Exception as e:
            logger.warning(f"SearXNG search failed: {e}, falling back to Ollama")
            
            if not self.ollama_service:
                logger.error("No Ollama service available for fallback")
                raise ValueError("SearXNG search failed and Ollama API key not configured")
            
            try:
                logger.info(f"Trying Ollama search for query: {query}")
                result = await self.ollama_service.web_search(query, max_results)
                logger.info(f"Ollama search succeeded, returned {len(result.get('results', []))} results")
                # Ensure result has engine field
                if "engine" not in result:
                    result["engine"] = "ollama"
                return result
            except Exception as fallback_error:
                logger.error(f"Both SearXNG and Ollama searches failed. Last error: {fallback_error}")
                raise
    
    async def close(self):
        """Close all HTTP clients"""
        await self.searxng_service.close()
        if self.ollama_service:
            await self.ollama_service.close()

