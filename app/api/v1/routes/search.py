"""
Public search endpoint using unified search (SearXNG primary, Ollama fallback)
"""
import logging
import os
from typing import Optional
from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from app.core.unified_search import UnifiedSearchService
from app.core.ollama_web_search import OllamaWebSearchService
from app.schema.settings import AppSettingsModel

logger = logging.getLogger(__name__)
router = APIRouter()

def get_unified_search_service(
    api_key: Optional[str] = None,
    api_key_2: Optional[str] = None,
    searxng_url: str = "http://localhost:7019"
) -> UnifiedSearchService:
    """Get or create unified search service instance"""
    # Fall back to env vars if no API keys provided
    if not api_key:
        api_key = os.getenv("OLLAMA_API_KEY")
    if not api_key_2:
        api_key_2 = os.getenv("OLLAMA_API_KEY_2")
    
    return UnifiedSearchService(
        searxng_url=searxng_url,
        ollama_api_key=api_key,
        ollama_api_key_2=api_key_2,
        timeout=20.0
    )

def get_ollama_search_service(api_key: Optional[str] = None, api_key_2: Optional[str] = None) -> OllamaWebSearchService:
    """Get or create Ollama web search service instance (for backward compatibility)"""
    if api_key or api_key_2:
        return OllamaWebSearchService(api_key=api_key, api_key_2=api_key_2)
    
    api_key = os.getenv("OLLAMA_API_KEY")
    api_key_2 = os.getenv("OLLAMA_API_KEY_2")
    
    if not api_key and not api_key_2:
        logger.warning("OLLAMA_API_KEY not set. Web search will not work.")
    
    return OllamaWebSearchService(api_key=api_key, api_key_2=api_key_2)


@router.get("/search", response_class=HTMLResponse)
async def public_search_html(
    request: Request,
    q: str = Query(..., description="Search query"),
    format: str = Query("html", description="Response format: html or json"),
    engine: Optional[str] = Query(None, description="Search engine: 'searxng', 'ollama', or 'auto' (default)")
):
    """
    Public search endpoint - can be used as browser default search engine.
    
    Usage in browser:
    - Add as search engine: http://localhost:8082/search?q=%s
    - Or use directly: http://localhost:8082/search?q=your+search+term
    """
    if not q or not q.strip():
        return HTMLResponse("""
        <html>
        <head><title>Search</title></head>
        <body>
            <h1>Search</h1>
            <p>Enter a search query in the URL: /search?q=your+query</p>
        </body>
        </html>
        """)
    
    # Perform search using unified search service
    try:
        # Try to get API keys and SearXNG URL from settings first
        api_key = None
        api_key_2 = None
        searxng_url = "http://localhost:7019"  # Default fallback
        try:
            app_settings: AppSettingsModel = request.app.state.settings
            api_key = app_settings.ollama_api_key
            api_key_2 = app_settings.ollama_api_key_2
            if app_settings.searxng_url:
                searxng_url = app_settings.searxng_url
        except:
            pass
        
        # Fall back to env vars if not in settings
        if not api_key:
            api_key = os.getenv("OLLAMA_API_KEY")
        if not api_key_2:
            api_key_2 = os.getenv("OLLAMA_API_KEY_2")
        
        search_service = get_unified_search_service(
            api_key=api_key,
            api_key_2=api_key_2,
            searxng_url=searxng_url
        )
        search_engine = engine if engine in ["searxng", "ollama"] else None
        search_response = await search_service.web_search(query=q.strip(), max_results=10, engine=search_engine)
        if "results" in search_response:
            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                    "engine": "ollama"
                }
                for r in search_response["results"]
            ]
        else:
            results = []
        engine_used = search_response.get("engine", "auto")
    except ValueError as e:
        logger.error(f"Search configuration error: {e}")
        results = []
        error_msg = "Search service not configured. Please configure SearXNG or Ollama API keys."
        engine_used = None
    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        results = []
        error_msg = f"Search failed: {str(e)}"
        engine_used = None
    else:
        error_msg = None
        engine_used = search_response.get("engine", "auto")
    
    # Return JSON if requested
    if format.lower() == "json":
        return JSONResponse({
            "query": q,
            "results": results,
            "count": len(results)
        })
    
    # Return HTML search results page
    results_html = ""
    if error_msg:
        results_html = f'<div style="color: #d32f2f; padding: 10px; background: #ffebee; border-radius: 4px; margin-bottom: 20px;">{error_msg}</div>'
    
    if results:
        for i, result in enumerate(results, 1):
            title = result.get("title", "No title")
            url = result.get("url", "#")
            content = result.get("content", "")[:200]
            engine = result.get("engine", "ollama")
            
            results_html += f"""
            <div style="margin-bottom: 20px; padding: 10px; border-bottom: 1px solid #ddd;">
                <h3 style="margin: 0 0 5px 0;">
                    <a href="{url}" target="_blank" style="color: #1a0dab; text-decoration: none; font-size: 18px;">
                        {title}
                    </a>
                </h3>
                <p style="color: #006621; margin: 0; font-size: 14px;">{url}</p>
                <p style="color: #545454; margin: 5px 0 0 0; font-size: 14px;">{content}</p>
                <span style="color: #999; font-size: 12px;">Powered by {engine_used if engine_used else 'Unknown'}</span>
            </div>
            """
    elif not error_msg:
        results_html = "<p>No results found. Try a different search query.</p>"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Search: {q}</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: Arial, sans-serif;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f5f5f5;
            }}
            .search-box {{
                background: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                margin-bottom: 20px;
            }}
            .search-box form {{
                display: flex;
                gap: 10px;
            }}
            .search-box input {{
                flex: 1;
                padding: 10px;
                font-size: 16px;
                border: 1px solid #ddd;
                border-radius: 4px;
            }}
            .search-box button {{
                padding: 10px 20px;
                font-size: 16px;
                background-color: #4285f4;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
            }}
            .results {{
                background: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            .result-count {{
                color: #666;
                margin-bottom: 15px;
                font-size: 14px;
            }}
        </style>
    </head>
    <body>
        <div class="search-box">
            <form method="GET" action="/search">
                <input type="text" name="q" value="{q}" placeholder="Search..." autofocus>
                <button type="submit">Search</button>
            </form>
        </div>
        <div class="results">
            <div class="result-count">Found {len(results)} results for "{q}"{f' (via {engine_used})' if engine_used else ''}</div>
            {results_html}
        </div>
    </body>
    </html>
    """
    
    return HTMLResponse(html_content)


@router.get("/search.json")
async def public_search_json(
    request: Request,
    q: str = Query(..., description="Search query"),
    max_results: int = Query(5, ge=1, le=10, description="Maximum results to return"),
    engine: Optional[str] = Query(None, description="Search engine: 'searxng', 'ollama', or 'auto' (default)")
):
    """JSON search endpoint for API usage using unified search (SearXNG primary, Ollama fallback)"""
    if not q or not q.strip():
        return JSONResponse({"error": "Query parameter 'q' is required"}, status_code=400)
    
    try:
        # Try to get API keys and SearXNG URL from settings first
        api_key = None
        api_key_2 = None
        searxng_url = "http://localhost:7019"  # Default fallback
        try:
            app_settings: AppSettingsModel = request.app.state.settings
            api_key = app_settings.ollama_api_key
            api_key_2 = app_settings.ollama_api_key_2
            if app_settings.searxng_url:
                searxng_url = app_settings.searxng_url
        except:
            pass
        
        # Fall back to env vars if not in settings
        if not api_key:
            api_key = os.getenv("OLLAMA_API_KEY")
        if not api_key_2:
            api_key_2 = os.getenv("OLLAMA_API_KEY_2")
        
        search_service = get_unified_search_service(
            api_key=api_key,
            api_key_2=api_key_2,
            searxng_url=searxng_url
        )
        search_engine = engine if engine in ["searxng", "ollama"] else None
        search_response = await search_service.web_search(query=q.strip(), max_results=max_results, engine=search_engine)
        if "results" in search_response:
            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                    "engine": r.get("engine", search_response.get("engine", "unknown"))
                }
                for r in search_response["results"]
            ]
            return JSONResponse({
                "query": q,
                "results": results,
                "count": len(results),
                "engine_used": search_response.get("engine", "auto")
            })
        else:
            return JSONResponse({"error": "No results from Ollama web search"}, status_code=500)
    except ValueError as e:
        logger.error(f"Ollama search configuration error: {e}")
        return JSONResponse({"error": "OLLAMA_API_KEY not configured"}, status_code=503)
    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/web_fetch")
async def web_fetch(
    request: Request,
    url: str = Query(..., description="URL to fetch")
):
    """Fetch a web page using Ollama's web_fetch API"""
    if not url or not url.strip():
        return JSONResponse({"error": "URL parameter is required"}, status_code=400)
    
    try:
        # Try to get API keys from settings first
        api_key = None
        api_key_2 = None
        try:
            app_settings: AppSettingsModel = request.app.state.settings
            api_key = app_settings.ollama_api_key
            api_key_2 = app_settings.ollama_api_key_2
        except:
            pass
        
        # Fall back to env vars if not in settings
        if not api_key:
            api_key = os.getenv("OLLAMA_API_KEY")
        if not api_key_2:
            api_key_2 = os.getenv("OLLAMA_API_KEY_2")
        
        search_service = get_ollama_search_service(api_key=api_key, api_key_2=api_key_2)
        fetch_response = await search_service.web_fetch(url=url.strip())
        return JSONResponse(fetch_response)
    except ValueError as e:
        logger.error(f"Ollama fetch configuration error: {e}")
        return JSONResponse({"error": "OLLAMA_API_KEY not configured"}, status_code=503)
    except Exception as e:
        logger.error(f"Fetch error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

