"""
Simple search proxy that doesn't require SearXNG.
Uses direct HTTP requests to search engines.
"""
import asyncio
import logging
import httpx
from typing import List, Dict, Any, Optional
import urllib.parse

logger = logging.getLogger(__name__)

class SimpleSearchProxy:
    """Simple search proxy that queries search engines directly"""
    
    def __init__(self):
        # Maximized timeouts for lenient speed expectations
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0, read=20.0, write=10.0, pool=30.0),
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=50)
        )
    
    async def search(self, query: str, engines: List[str] = None) -> List[Dict[str, Any]]:
        """
        Perform a search query with all available engines enabled
        
        Args:
            query: Search query string
            engines: List of engines to use (default: all available)
        
        Returns:
            List of search results (may be empty if search fails - system works without it)
        """
        if engines is None:
            # Enable ALL available engines
            engines = ['duckduckgo', 'bing', 'brave', 'startpage']
        
        results = []
        
        # Run all searches in parallel for maximum speed
        search_tasks = []
        
        if 'duckduckgo' in engines:
            search_tasks.append(self._search_duckduckgo(query))
        
        if 'bing' in engines:
            search_tasks.append(self._search_bing(query))
        
        if 'brave' in engines:
            search_tasks.append(self._search_brave(query))
        
        if 'startpage' in engines:
            search_tasks.append(self._search_startpage(query))
        
        # Execute all searches in parallel
        if search_tasks:
            try:
                search_results = await asyncio.gather(*search_tasks, return_exceptions=True)
                for engine_results in search_results:
                    if isinstance(engine_results, list):
                        results.extend(engine_results)
                    elif isinstance(engine_results, Exception):
                        logger.debug(f"Search engine error: {engine_results}")
            except Exception as e:
                logger.debug(f"Parallel search error: {e}")
        
        # Remove duplicates by URL
        seen_urls = set()
        unique_results = []
        for result in results:
            url = result.get('url', '')
            if url and url not in seen_urls and url.startswith('http'):
                seen_urls.add(url)
                unique_results.append(result)
        
        logger.info(f"Search for '{query}' returned {len(unique_results)} unique results from {len(engines)} engines")
        
        # Note: Returning empty results is OK - description generation works without search
        return unique_results[:15]  # Increased limit to 15 results
    
    async def _search_duckduckgo(self, query: str) -> List[Dict[str, Any]]:
        """Search using DuckDuckGo - try multiple methods with maximum coverage"""
        results = []
        
        # Method 1: DuckDuckGo Instant Answer API (fast, structured)
        try:
            url = "https://api.duckduckgo.com/"
            params = {
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1"
            }
            
            response = await self.client.get(url, params=params, timeout=15.0)
            if response.status_code == 200:
                data = response.json()
                
                # Extract abstract if available
                if data.get("AbstractText"):
                    results.append({
                        "title": data.get("Heading", query),
                        "url": data.get("AbstractURL", ""),
                        "content": data.get("AbstractText", "")[:300],
                        "engine": "duckduckgo"
                    })
                
                # Extract related topics
                for topic in data.get("RelatedTopics", [])[:8]:
                    if isinstance(topic, dict) and "Text" in topic:
                        text = topic.get("Text", "")
                        title = text.split(" - ")[0] if " - " in text else text[:150]
                        first_url = topic.get("FirstURL", "")
                        if first_url:
                            results.append({
                                "title": title,
                                "url": first_url,
                                "content": text[:300],
                                "engine": "duckduckgo"
                            })
        except Exception as e:
            logger.debug(f"DuckDuckGo API error: {e}")
        
        # Method 2: DuckDuckGo HTML search (more results)
        try:
            # Use DuckDuckGo's HTML interface
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"
            }
            response = await self.client.get(url, headers=headers, follow_redirects=True, timeout=20.0)
            
            if response.status_code == 200:
                html = response.text
                import re
                # Multiple patterns for DuckDuckGo HTML results
                patterns = [
                    r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>',  # Standard results
                    r'<a[^>]*class="[^"]*result-link[^"]*"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>',  # Lite results
                    r'<a[^>]*href="([^"]*)"[^>]*class="[^"]*result[^"]*"[^>]*>([^<]*)</a>',  # Alternative
                ]
                
                for pattern in patterns:
                    matches = re.findall(pattern, html, re.IGNORECASE)
                    for url_match, title in matches[:10]:
                        if url_match.startswith('http') and url_match not in [r.get('url', '') for r in results]:
                            # Try to extract snippet
                            snippet_match = re.search(
                                rf'<a[^>]*href="{re.escape(url_match)}"[^>]*>.*?</a>\s*<[^>]*>([^<]{{50,200}})</',
                                html,
                                re.IGNORECASE | re.DOTALL
                            )
                            snippet = snippet_match.group(1).strip()[:200] if snippet_match else f"DuckDuckGo result for {query}"
                            
                            results.append({
                                "title": title.strip()[:150],
                                "url": url_match,
                                "content": snippet,
                                "engine": "duckduckgo"
                            })
                    if len(results) >= 10:
                        break
        except Exception as e:
            logger.debug(f"DuckDuckGo HTML search error: {e}")
        
        # Method 3: DuckDuckGo Lite (fallback, most reliable)
        if len(results) < 5:
            try:
                url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html"
                }
                response = await self.client.get(url, headers=headers, timeout=20.0)
                
                if response.status_code == 200:
                    html = response.text
                    import re
                    # DuckDuckGo lite uses simple structure
                    link_pattern = r'<a[^>]*class="result-link"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>'
                    matches = re.findall(link_pattern, html)
                    
                    for url_match, title in matches[:8]:
                        if url_match.startswith('http') and url_match not in [r.get('url', '') for r in results]:
                            results.append({
                                "title": title.strip()[:150],
                                "url": url_match,
                                "content": f"DuckDuckGo Lite result for {query}",
                                "engine": "duckduckgo"
                            })
            except Exception as e:
                logger.debug(f"DuckDuckGo Lite search error: {e}")
        
        return results
    
    async def _search_bing(self, query: str) -> List[Dict[str, Any]]:
        """Search using Bing (HTML scraping)"""
        results = []
        try:
            url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"
            }
            response = await self.client.get(url, headers=headers, timeout=20.0)
            
            if response.status_code == 200:
                html = response.text
                import re
                # Bing result pattern: <h2><a href="..." ...>title</a></h2>
                link_pattern = r'<h2><a[^>]*href="([^"]*)"[^>]*>([^<]*)</a></h2>'
                matches = re.findall(link_pattern, html, re.IGNORECASE)
                
                for url_match, title in matches[:5]:
                    if url_match.startswith('http'):
                        # Try to find snippet
                        snippet = ""
                        results.append({
                            "title": title.strip()[:150],
                            "url": url_match,
                            "content": snippet or f"Bing search result for {query}",
                            "engine": "bing"
                        })
        except Exception as e:
            logger.debug(f"Bing search error: {e}")
        
        return results
    
    async def _search_brave(self, query: str) -> List[Dict[str, Any]]:
        """Search using Brave Search API (if available) or HTML"""
        results = []
        try:
            # Try Brave Search API first (requires API key, but try anyway)
            # For now, use HTML interface
            url = f"https://search.brave.com/search?q={urllib.parse.quote(query)}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = await self.client.get(url, headers=headers, timeout=20.0)
            
            if response.status_code == 200:
                html = response.text
                import re
                # Brave result pattern (simplified)
                link_pattern = r'<a[^>]*class="[^"]*result[^"]*"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>'
                matches = re.findall(link_pattern, html, re.IGNORECASE)
                
                for url_match, title in matches[:5]:
                    if url_match.startswith('http'):
                        results.append({
                            "title": title.strip()[:150],
                            "url": url_match,
                            "content": f"Brave search result for {query}",
                            "engine": "brave"
                        })
        except Exception as e:
            logger.debug(f"Brave search error: {e}")
        
        return results
    
    async def _search_startpage(self, query: str) -> List[Dict[str, Any]]:
        """Search using Startpage (privacy-focused Google proxy)"""
        results = []
        try:
            url = f"https://www.startpage.com/sp/search?query={urllib.parse.quote(query)}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = await self.client.get(url, headers=headers, timeout=20.0)
            
            if response.status_code == 200:
                html = response.text
                import re
                # Startpage result pattern
                link_pattern = r'<a[^>]*class="[^"]*w-gl[^"]*"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>'
                matches = re.findall(link_pattern, html, re.IGNORECASE)
                
                for url_match, title in matches[:5]:
                    if url_match.startswith('http'):
                        results.append({
                            "title": title.strip()[:150],
                            "url": url_match,
                            "content": f"Startpage search result for {query}",
                            "engine": "startpage"
                        })
        except Exception as e:
            logger.debug(f"Startpage search error: {e}")
        
        return results
    
    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose()

# Global instance
_search_proxy: Optional[SimpleSearchProxy] = None

async def get_search_proxy() -> SimpleSearchProxy:
    """Get or create the search proxy instance"""
    global _search_proxy
    if _search_proxy is None:
        _search_proxy = SimpleSearchProxy()
    return _search_proxy

async def search(query: str) -> List[Dict[str, Any]]:
    """Convenience function to perform a search"""
    proxy = await get_search_proxy()
    return await proxy.search(query)

