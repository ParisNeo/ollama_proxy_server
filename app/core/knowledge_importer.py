import io
import re
import logging
import base64
import secrets
import httpx
from typing import List, Dict, Any, Optional
from fastapi import UploadFile
import pipmaster as pm

logger = logging.getLogger(__name__)

async def extract_local_file_content(files: List[UploadFile]) -> str:
    """Parses local uploaded files (PDF, Docx, Images) into text."""
    pm.ensure_packages(["pypdf", "python-docx", "pdf2image", "Pillow"], verbose=True)
    import pypdf
    import docx
    
    extracted = []
    for file in files:
        data = await file.read()
        if file.content_type.startswith("image/"):
            b64 = base64.b64encode(data).decode('utf-8')
            extracted.append(f"IMAGE: {file.filename}\n[img:{b64}]")
        elif file.filename.endswith(".pdf"):
            reader = pypdf.PdfReader(io.BytesIO(data))
            text = "\n".join([p.extract_text() for p in reader.pages])
            extracted.append(f"PDF: {file.filename}\n{text}")
        elif file.filename.endswith(".docx"):
            doc = docx.Document(io.BytesIO(data))
            text = "\n".join([p.text for p in doc.paragraphs])
            extracted.append(f"DOCX: {file.filename}\n{text}")
        else:
            # Fallback for text files
            try:
                text = data.decode('utf-8')
                extracted.append(f"FILE: {file.filename}\n{text}")
            except: pass
    return "\n\n".join(extracted)

def search_wikipedia_sync(query: str) -> List[Dict[str, Any]]:
    pm.ensure_packages(["wikipedia"], verbose=True)
    import wikipedia
    results = []
    search_results = wikipedia.search(query)
    for title in search_results[:5]:
        try:
            page = wikipedia.page(title, auto_suggest=False)
            results.append({
                "title": page.title, 
                "snippet": page.summary[:300] + "...", 
                "url": page.url, 
                "content": page.content
            })
        except: continue
    return results

def search_arxiv_sync(
    query: str, 
    author: Optional[str] = None, 
    year: Optional[str] = None, 
    max_results: int = 5,
    full_content: bool = False
) -> List[Dict[str, Any]]:
    pm.ensure_packages(["arxiv", "pypdf"], verbose=True)
    import arxiv
    import pypdf
    
    full_query = query
    if author: full_query += f" AND au:{author}"
    if year: full_query += f" AND jr:{year}"

    client = arxiv.Client()
    search = arxiv.Search(query=full_query, max_results=max_results, sort_by=arxiv.SortCriterion.Relevance)
    results = []
    
    for res in client.results(search):
        # FIX: Join author names correctly
        author_names = ", ".join([a.name for a in res.authors])
        content = f"Title: {res.title}\nAuthors: {author_names}\nAbstract: {res.summary}"
        
        if full_content:
            try:
                # Use synchronous httpx inside the sync helper
                resp = httpx.get(res.pdf_url, follow_redirects=True, timeout=30.0)
                pdf_file = io.BytesIO(resp.content)
                reader = pypdf.PdfReader(pdf_file)
                pdf_text = "\n".join([page.extract_text() for page in reader.pages])
                content += f"\n\n--- FULL PAPER CONTENT ---\n{pdf_text}"
            except Exception as e:
                content += f"\n\n[Error extracting full text: {str(e)}]"
        
        results.append({
            "title": res.title, 
            "snippet": res.summary[:300] + "...", 
            "url": res.pdf_url, 
            "content": content
        })
    return results

def fetch_youtube_transcript_sync(video_id_or_url: str, languages: List[str] = ['en']) -> List[Dict[str, Any]]:
    """
    Uses ScrapeMaster to import YouTube transcripts with built-in language preference.
    """
    pm.ensure_packages(["scrapemaster"], verbose=True)
    from scrapemaster import ScrapeMaster
    
    scraper = ScrapeMaster()
    try:
        lang_code = languages[0] if languages else 'en'
        transcript = scraper.scrape_youtube_transcript(video_id_or_url, language_code=lang_code)
        
        if not transcript:
            raise RuntimeError("No transcript available or could not be reached.")

        return [{
            "title": f"YouTube Transcript",
            "snippet": transcript[:300] + "...",
            "content": transcript
        }]
    except Exception as e:
        logger.error(f"YouTube ScrapeMaster failed: {e}")
        raise RuntimeError(f"YouTube Import Failed: {str(e)}")

def search_google_sync(query: str) -> List[Dict[str, Any]]:
    """Performs a web search via Google."""
    pm.ensure_packages(["googlesearch-python"], verbose=True)
    from googlesearch import search
    results = []
    try:
        for url in search(query, num_results=5):
            results.append({
                "title": url, 
                "snippet": "Web result from Google search. Select to fetch content.", 
                "url": url, 
                "content": url, 
                "is_url": True
            })
    except Exception as e:
        logger.error(f"Google Search Error: {e}")
    return results

async def scrape_url(url: str, depth: int = 0) -> Dict[str, Any]:
    """
    Uses ScrapeMaster to extract clean Markdown content from a URL.
    Handles fallbacks for JS-heavy sites and anti-bot measures.
    """
    pm.ensure_packages(["scrapemaster"], verbose=True)
    from scrapemaster import ScrapeMaster
    
    try:
        # Initialize ScrapeMaster for the target URL
        scraper = ScrapeMaster(url)
        
        # Scrape to Markdown (best for LLM context)
        # This automatically cycles through Requests -> Selenium -> Undetected if blocked
        content_md = scraper.scrape_markdown()
        
        if not content_md and scraper.last_error:
            logger.error(f"ScrapeMaster failed: {scraper.last_error}")
            return {"title": url, "content": f"Error: {scraper.last_error}"}

        return {
            "title": scraper.title or url,
            "content": content_md or "",
            "strategy": scraper.last_strategy_used
        }
    except Exception as e:
        logger.error(f"ScrapeMaster exception: {e}")
        return {"title": url, "content": f"Scraper Exception: {str(e)}"}