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
    Robustly imports YouTube transcripts using smart language selection.
    Priority: Requested -> Translated -> English -> First Available.
    """
    # 0. Ensure library is available AND forced to upgrade to handle latest YouTube HTML changes
    pm.ensure_packages({"youtube-transcript-api": ">=1.2.4"})
    from youtube_transcript_api import YouTubeTranscriptApi
    
    # 1. Hardened Video ID Extraction (matches working snippet)
    video_id = None
    patterns = [
        r'(?:v=|\/|shorts\/)([0-9A-Za-z_-]{11}).*', # Added shorts/ explicitly for safety
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})', 
        r'(?:embed\/)([0-9A-Za-z_-]{11})'
    ]
    
    for p in patterns:
         match = re.search(p, video_id_or_url)
         if match:
             video_id = match.group(1)
             break
    
    # Fallback: check if the whole string is an ID
    if not video_id:
         if len(video_id_or_url) == 11 and re.match(r'^[0-9A-Za-z_-]{11}$', video_id_or_url):
             video_id = video_id_or_url
    
    if not video_id:
        raise ValueError("Could not extract a valid YouTube Video ID from the provided URL.")

    try:
        # 2. Retrieve Transcript List
        try:
            yvt = YouTubeTranscriptApi()
            transcript_list_obj = yvt.list(video_id)
        except Exception as e:
            raise RuntimeError(f"Failed to retrieve transcript list. Video may not have captions or is restricted. Error: {e}")

        target_transcript = None
        requested_lang = (languages[0] if languages else 'en').lower().strip()

        # 3. Smart Selection Logic
        if requested_lang:
            # Try finding exact match
            try:
                target_transcript = transcript_list_obj.find_transcript([requested_lang])
            except:
                # Try finding a translation
                try:
                    # Translate the first available transcript
                    first_available = next(iter(transcript_list_obj))
                    if first_available.is_translatable:
                        target_transcript = first_available.translate(requested_lang)
                except:
                    pass 
        
        # If no specific language requested OR specific lookup failed
        if not target_transcript:
            # Priority: English -> First Available
            try:
                target_transcript = transcript_list_obj.find_generated_transcript(['en'])
            except:
                pass
            
            if not target_transcript:
                try:
                    target_transcript = transcript_list_obj.find_manually_created_transcript(['en'])
                except:
                    pass
            
            if not target_transcript:
                try:
                    target_transcript = next(iter(transcript_list_obj))
                except:
                    pass

        if not target_transcript:
            raise RuntimeError("No suitable transcript found.")

        # 4. Fetch
        transcript_data = target_transcript.fetch()

        # 5. Format[MM:SS]
        lines =[]
        # Support both standard list of dicts and custom objects (like .snippets)
        entries = transcript_data.snippets if hasattr(transcript_data, 'snippets') else transcript_data
        
        for entry in entries:
            start = int(entry.start if hasattr(entry, 'start') else entry.get('start', 0))
            text = entry.text if hasattr(entry, 'text') else entry.get('text', '')
            minutes = start // 60
            seconds = start % 60
            lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")
        
        lang_label = getattr(target_transcript, 'language', requested_lang)
        full_content = f"# YouTube Transcript ({lang_label})\nSource: {video_id_or_url}\n\n" + "\n".join(lines)
        
        return[{
            "title": f"YouTube: {video_id}", 
            "snippet": " ".join(lines[:3]), 
            "content": full_content
        }]

    except Exception as e:
        err_msg = str(e)
        if "no element found" in err_msg.lower():
            err_msg = "YouTube returned an empty response. Ensure the package upgraded correctly, or the video might be restricted."
        raise RuntimeError(f"YouTube Import Failed: {err_msg}")

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
    pm.ensure_packages(["scrapemaster"], verbose=True)
    from scrapemaster import WebScraper
    scraper = WebScraper(respect_robots_txt=True)
    data = scraper.scrape_url(url, {'content': 'body::text', 'title': 'title::text'})
    return {"title": data.get('title') or url, "content": data.get('content') or ""}