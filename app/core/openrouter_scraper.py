"""
OpenRouter Model Scraper using Playwright
Scrapes OpenRouter's model listing page to get official capability information
"""
import logging
import asyncio
import time
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlencode
from collections import OrderedDict

logger = logging.getLogger(__name__)

# Simple in-memory cache with TTL (Time-To-Live)
# Cache structure: {model_name: (data, timestamp)}
_openrouter_cache: Dict[str, Tuple[Dict[str, Any], float]] = OrderedDict()
CACHE_TTL = 3600  # 1 hour in seconds
CACHE_MAX_SIZE = 100  # Maximum number of cached entries


def _get_cached(model_name: str) -> Optional[Dict[str, Any]]:
    """Get cached data if it exists and hasn't expired."""
    if model_name not in _openrouter_cache:
        return None
    
    data, timestamp = _openrouter_cache[model_name]
    if time.time() - timestamp > CACHE_TTL:
        # Expired, remove from cache
        del _openrouter_cache[model_name]
        return None
    
    # Move to end (LRU)
    _openrouter_cache.move_to_end(model_name)
    logger.debug(f"Cache hit for model '{model_name}'")
    return data


def _set_cached(model_name: str, data: Dict[str, Any]) -> None:
    """Cache data with current timestamp."""
    # Remove oldest entry if cache is full
    if len(_openrouter_cache) >= CACHE_MAX_SIZE:
        _openrouter_cache.popitem(last=False)  # Remove oldest (FIFO)
    
    _openrouter_cache[model_name] = (data, time.time())
    _openrouter_cache.move_to_end(model_name)  # Move to end (LRU)
    logger.debug(f"Cached data for model '{model_name}'")


def _clear_expired_cache() -> None:
    """Remove expired entries from cache."""
    current_time = time.time()
    expired_keys = [
        key for key, (_, timestamp) in _openrouter_cache.items()
        if current_time - timestamp > CACHE_TTL
    ]
    for key in expired_keys:
        del _openrouter_cache[key]
    if expired_keys:
        logger.debug(f"Cleared {len(expired_keys)} expired cache entries")


def clear_openrouter_cache(model_name: Optional[str] = None) -> int:
    """
    Clear cache entries. If model_name is provided, clears only that entry.
    Otherwise, clears all entries.
    
    Args:
        model_name: Optional specific model to clear, or None to clear all
    
    Returns:
        Number of entries cleared
    """
    global _openrouter_cache
    if model_name:
        if model_name in _openrouter_cache:
            del _openrouter_cache[model_name]
            logger.info(f"Cleared cache for model '{model_name}'")
            return 1
        return 0
    else:
        count = len(_openrouter_cache)
        _openrouter_cache.clear()
        logger.info(f"Cleared all {count} cache entries")
        return count


def get_cache_stats() -> Dict[str, Any]:
    """
    Get cache statistics.
    
    Returns:
        Dict with cache statistics
    """
    current_time = time.time()
    total_entries = len(_openrouter_cache)
    expired_count = sum(
        1 for _, timestamp in _openrouter_cache.values()
        if current_time - timestamp > CACHE_TTL
    )
    valid_count = total_entries - expired_count
    
    return {
        "total_entries": total_entries,
        "valid_entries": valid_count,
        "expired_entries": expired_count,
        "max_size": CACHE_MAX_SIZE,
        "ttl_seconds": CACHE_TTL,
        "cache_keys": list(_openrouter_cache.keys())
    }

# Try to import Playwright, but don't fail if not installed
try:
    from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not installed. OpenRouter scraping will be disabled. Install with: pip install playwright && playwright install chromium")


async def scrape_openrouter_model_info(
    model_name: str,
    server_type: str = "openrouter",
    max_retries: int = 3,
    retry_delay: float = 1.0
) -> Optional[Dict[str, Any]]:
    """
    Scrape OpenRouter's model listing page to get official capability information.
    Uses caching and retry logic for reliability.
    
    Args:
        model_name: Model name (e.g., "google/gemini-3-pro-preview" or "openai/gpt-5.1")
        server_type: Server type ("openrouter" or "ollama")
        max_retries: Maximum number of retry attempts (default: 3)
        retry_delay: Initial delay between retries in seconds, doubles each retry (default: 1.0)
    
    Returns:
        Dict with model capabilities including:
        - supports_web_search: bool (from web_search_options parameter)
        - supports_images: bool
        - supports_tools: bool
        - context_length: int
        - pricing: dict
        - official_description: str
        Or None if scraping fails
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.debug("Playwright not available, skipping OpenRouter scraping")
        return None
    
    if server_type != "openrouter":
        # Only scrape OpenRouter models
        return None
    
    # Check cache first
    _clear_expired_cache()
    cached_data = _get_cached(model_name)
    if cached_data:
        return cached_data
    
    # Retry logic with exponential backoff
    last_exception = None
    for attempt in range(max_retries):
        try:
            result = await _scrape_openrouter_page(model_name)
            if result:
                # Cache successful result
                _set_cached(model_name, result)
                return result
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)  # Exponential backoff
                logger.warning(f"Scraping attempt {attempt + 1} failed for '{model_name}': {e}. Retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)
            else:
                logger.error(f"All {max_retries} scraping attempts failed for '{model_name}': {e}")
    
    return None


async def _scrape_openrouter_page(model_name: str) -> Optional[Dict[str, Any]]:
    """
    Internal function to perform the actual scraping.
    Separated for retry logic.
    """
    try:
        async with async_playwright() as p:
            # Launch browser in headless mode
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()
            
            # Navigate to OpenRouter models page filtered by web_search_options
            # This shows models that support web search
            base_url = "https://openrouter.ai/models"
            params = {
                "fmt": "cards",
                "supported_parameters": "web_search_options",
                "order": "newest"
            }
            url = f"{base_url}?{urlencode(params)}"
            
            logger.info(f"Scraping OpenRouter for model '{model_name}' from {url}")
            
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Wait for model cards to be rendered (OpenRouter uses dynamic loading)
                await page.wait_for_selector('body', timeout=10000)
                # Scroll to load more content
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(3000)  # Wait for dynamic content to load
                # Scroll back up
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(1000)
            except PlaywrightTimeout:
                logger.warning(f"Timeout loading OpenRouter page for {model_name}")
                await browser.close()
                return None
            
            # Extract model information from the page
            model_info = None
            
            # Try to find the model card by searching for the model name
            # OpenRouter uses dynamic rendering, so we need to search through all model cards
            try:
                    # Wait for any content to load - OpenRouter may use various selectors
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    
                    # Try multiple selectors to find model cards
                    # OpenRouter's structure may vary, so we try common patterns
                    # Expanded selector list for better reliability
                    selectors = [
                        # Modern React/Next.js patterns
                        'article[class*="model"]',
                        'div[class*="ModelCard"]',
                        'div[class*="model-card"]',
                        'div[class*="Model"]',
                        'a[href*="/models/"]',
                        'div[data-model-id]',
                        'div[data-testid*="model"]',
                        # Generic card patterns
                        'article',
                        'div[class*="card"]',
                        'div[class*="Card"]',
                        '[class*="model"]',
                        '[class*="Model"]',
                        # Link-based patterns
                        'a[href*="model"]',
                        'a[href*="Model"]',
                        # Grid/list item patterns
                        'li[class*="model"]',
                        'div[role="article"]',
                        'div[role="listitem"]',
                        # Fallback: any element with model in class or data attribute
                        '[class*="model"]',
                        '[data-model]',
                        '[data-id*="model"]'
                    ]
                    
                    model_cards = []
                    for selector in selectors:
                        try:
                            cards = await page.query_selector_all(selector)
                            if cards:
                                model_cards = cards
                                logger.debug(f"Found {len(cards)} elements using selector: {selector}")
                                break
                        except:
                            continue
                    
                    # If no cards found, try getting all links to model pages
                    if not model_cards:
                        model_cards = await page.query_selector_all('a[href*="/models/"]')
                
                logger.info(f"Found {len(model_cards)} model cards on OpenRouter page")
                
                # Search for our specific model
                model_name_lower = model_name.lower()
                model_id_lower = model_name_lower.replace("/", "-")
                
                for card in model_cards:
                    try:
                        # Get text content of the card
                        card_text = await card.inner_text()
                        card_html = await card.inner_html()
                        
                        # Check if this card contains our model name
                        if model_name_lower in card_text.lower() or model_id_lower in card_text.lower():
                            logger.info(f"Found model card for '{model_name}'")
                            
                            # Extract capabilities from the card
                            model_info = {
                                "model_name": model_name,
                                "supports_web_search": "web_search" in card_html.lower() or "web search" in card_text.lower(),
                                "supports_images": "image" in card_text.lower() or "vision" in card_text.lower() or "multimodal" in card_text.lower(),
                                "supports_tools": "tool" in card_text.lower() or "function" in card_text.lower(),
                                "official_description": card_text[:500] if card_text else "",  # First 500 chars
                                "source": "openrouter_scraped"
                            }
                            
                            # Try to extract context length
                            import re
                            ctx_match = re.search(r'(\d+[kK]?)\s*(?:token|context)', card_text, re.IGNORECASE)
                            if ctx_match:
                                ctx_str = ctx_match.group(1)
                                if 'k' in ctx_str.lower():
                                    model_info["context_length"] = int(ctx_str.lower().replace('k', '')) * 1000
                                else:
                                    model_info["context_length"] = int(ctx_str)
                            
                            # Try to extract pricing
                            price_match = re.search(r'\$?([\d.]+)\s*/\s*1[Mm]', card_text)
                            if price_match:
                                model_info["pricing_hint"] = f"${price_match.group(1)}/1M tokens"
                            
                            break
                    except Exception as e:
                        logger.debug(f"Error processing model card: {e}")
                        continue
                
                # If not found in web_search filtered page, try the general models page
                if not model_info:
                    logger.info(f"Model '{model_name}' not found in web_search filtered page, trying general search")
                    search_url = f"https://openrouter.ai/models?q={model_name.replace('/', '%2F')}"
                    try:
                        await page.goto(search_url, wait_until="networkidle", timeout=30000)
                        await page.wait_for_timeout(2000)
                        
                        # Search again
                        model_cards = await page.query_selector_all('article, [class*="model-card"], [class*="model-item"], div[class*="card"]')
                        for card in model_cards:
                            card_text = await card.inner_text()
                            card_html = await card.inner_html()
                            if model_name_lower in card_text.lower():
                                model_info = {
                                    "model_name": model_name,
                                    "supports_web_search": "web_search" in card_html.lower() or "web search" in card_text.lower(),
                                    "supports_images": "image" in card_text.lower() or "vision" in card_text.lower(),
                                    "supports_tools": "tool" in card_text.lower() or "function" in card_text.lower(),
                                    "official_description": card_text[:500] if card_text else "",
                                    "source": "openrouter_scraped"
                                }
                                break
                    except Exception as e:
                        logger.debug(f"Error searching general page: {e}")
            except Exception as e:
                logger.warning(f"Error extracting model info from OpenRouter page: {e}")
            
            await browser.close()
            
            if model_info:
                logger.info(f"Successfully scraped OpenRouter info for '{model_name}': web_search={model_info.get('supports_web_search')}")
            
            return model_info
            
    except Exception as e:
        logger.error(f"Failed to scrape OpenRouter page for model '{model_name}': {e}", exc_info=True)
        raise  # Re-raise for retry logic


async def get_openrouter_capabilities_from_api(
    model_name: str,
    api_key: Optional[str] = None,
    max_retries: int = 3,
    retry_delay: float = 0.5
) -> Optional[Dict[str, Any]]:
    """
    Alternative: Get model capabilities from OpenRouter API directly.
    This is faster than scraping but may have less detail.
    Uses caching and retry logic for reliability.
    
    Args:
        model_name: Model name (e.g., "google/gemini-3-pro-preview")
        api_key: Optional OpenRouter API key
        max_retries: Maximum number of retry attempts (default: 3)
        retry_delay: Initial delay between retries in seconds, doubles each retry (default: 0.5)
    
    Returns:
        Dict with model capabilities or None
    """
    # Check cache first (API results cached separately with "api" suffix)
    cache_key = f"{model_name}_api"
    _clear_expired_cache()
    cached_data = _get_cached(cache_key)
    if cached_data:
        return cached_data
    
    # Retry logic with exponential backoff
    last_exception = None
    for attempt in range(max_retries):
        try:
            import httpx
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Get all models
                response = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers=headers
                )
                response.raise_for_status()
                data = response.json()
                
                # Find our model
                models = data.get("data", [])
                for model in models:
                    if model.get("id") == model_name or model.get("name") == model_name:
                        # Extract capabilities
                        capabilities = model.get("capabilities", {})
                        supported_params = model.get("supported_parameters", [])
                        
                        result = {
                            "model_name": model_name,
                            "supports_web_search": "web_search_options" in supported_params or "web_search" in str(supported_params).lower(),
                            "supports_images": capabilities.get("image", False) or "vision" in str(capabilities).lower(),
                            "supports_tools": "tools" in supported_params or "function" in str(supported_params).lower(),
                            "context_length": model.get("context_length"),
                            "pricing": model.get("pricing", {}),
                            "description": model.get("description", ""),
                            "source": "openrouter_api"
                        }
                        
                        # Cache successful result
                        _set_cached(cache_key, result)
                        return result
                
                # Model not found - cache None result for shorter time to allow retries
                logger.debug(f"Model '{model_name}' not found in OpenRouter API")
                return None
                
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)  # Exponential backoff
                logger.warning(f"API request attempt {attempt + 1} failed for '{model_name}': {e}. Retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)
            else:
                logger.debug(f"All {max_retries} API attempts failed for '{model_name}': {e}")
    
    return None

