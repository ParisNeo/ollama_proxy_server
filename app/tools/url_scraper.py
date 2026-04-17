TOOL_LIBRARY_NAME = 'URL Scraper'
TOOL_LIBRARY_DESC = 'Extract clean text and markdown content from web pages.'
TOOL_LIBRARY_ICON = '🕸️'

def init_tool_library() -> None:
    import pipmaster as pm
    pm.ensure_packages({'scrapemaster': '>=0.1.0'})

def tool_scrape_url(args: dict, lollms=None):
    '''
    Extract clean markdown content from a given URL.
    
    Args:
        args: dict with keys:
            - url (str): The web page URL to scrape.
    '''
    try:
        url = args.get('url')
        if not url:
            return "Error: No URL provided."
            
        from scrapemaster import WebScraper
        scraper = WebScraper(respect_robots_txt=True)
        content = scraper.scrape_markdown(url)
        
        if not content and scraper.last_error:
            return f"Scrape Error: {scraper.last_error}"
            
        return f"--- SCRAPED CONTENT FROM {url} ---\n\n{content}"
    except Exception as e:
        return f"Execution Error: {str(e)}"