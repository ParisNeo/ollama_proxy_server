import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.database.models import User
from app.api.v1.routes.admin import require_admin_user
from app.core import knowledge_importer as kit

router = APIRouter()
logger = logging.getLogger(__name__)

class SearchRequest(BaseModel):
    query: str
    provider: str
    author: Optional[str] = None
    year: Optional[str] = None
    count: int = 5
    language: str = "en"
    full_content: bool = False

@router.post("/search", name="api_importer_search")
async def api_importer_search(request_data: SearchRequest, admin_user: User = Depends(require_admin_user)):
    try:
        if request_data.provider == 'wikipedia':
            return await run_in_threadpool(kit.search_wikipedia_sync, request_data.query)
        
        elif request_data.provider == 'arxiv':
            return await run_in_threadpool(
                kit.search_arxiv_sync,
                request_data.query, 
                request_data.author, 
                request_data.year, 
                request_data.count,
                request_data.full_content
            )
            
        elif request_data.provider == 'youtube':
            # Default to 'en' if not provided
            lang = request_data.language if request_data.language else "en"
            return await run_in_threadpool(
                kit.fetch_youtube_transcript_sync, 
                request_data.query, 
                [lang]
            )
        
        elif request_data.provider == 'google':
            return await run_in_threadpool(kit.search_google_sync, request_data.query)
            
        return []
    except Exception as e:
        logger.error(f"Importer Search Error: {e}")
        # Detect if it's a known provider error to avoid 500 status code
        status_code = 400 if "YouTube" in str(e) or "extract" in str(e) else 500
        return JSONResponse(
            {"error": str(e)}, 
            status_code=status_code
        )

@router.post("/scrape", name="api_importer_scrape")
async def api_importer_scrape(url: str = Form(...), depth: int = Form(0), admin_user: User = Depends(require_admin_user)):
    try:
        return await kit.scrape_url(url, depth)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)