TOOL_LIBRARY_NAME = 'Multi-Provider Web Search'
TOOL_LIBRARY_DESC = 'Search the internet with choice of providers (Free or Paid).'
TOOL_LIBRARY_ICON = '🌐'

TOOL_TITLES = {
    "tool_web_search": "🔍 Searching the web"
}

# LTP Configuration Metadata: Defined for the Tool Selector Node UI
TOOL_SETTINGS_METADATA = [
    {
        "name": "provider", 
        "type": "combo", 
        "options": ["DuckDuckGo (Free)", "Brave Search (Paid/Key Required)"], 
        "default": "DuckDuckGo (Free)"
    },
    {
        "name": "brave_api_key", 
        "type": "password", 
        "description": "Required only if using Brave Search."
    },
    {
        "name": "max_results", 
        "type": "number", 
        "default": 5
    }
]

def init_tool_library() -> None:
    import pipmaster as pm
    pm.ensure_packages({'duckduckgo-search': '>=6.0.0', 'httpx': '>=0.27.0'})

def tool_web_search(args: dict, lollms=None):
    '''
    Search the web for up-to-the-minute information.
    
    Args:
        args: dict with keys:
            - query (str): The search phrase.
    '''
    # Settings are injected by the Hub into the lollms object from the Node properties
    provider = lollms.get_setting('provider', 'DuckDuckGo (Free)')
    limit = int(lollms.get_setting('max_results', 5))
    query = args.get('query')

    if "Brave" in provider:
        import httpx
        key = lollms.get_setting('brave_api_key')
        if not key: return "Error: Brave API Key is missing. Configure it in the Tool Selector node."
        try:
            headers = {"Accept": "application/json", "X-Subscription-Token": key}
            resp = httpx.get(f"https://api.search.brave.com/res/v1/web/search?q={query}", headers=headers, timeout=10)
            data = resp.json()
            return "\n\n".join([f"[{r['title']}]({r['url']})\n{r['description']}" for r in data.get('web', {}).get('results', [])[:limit]])
        except Exception as e: return f"Brave Search Error: {str(e)}"

    # Default Free path
    from duckduckgo_search import DDGS
    try:
        with DDGS() as ddgs:
            results = [f"{r['title']}\nURL: {r['href']}\nSnippet: {r['body']}" for r in ddgs.text(query, max_results=limit)]
            return "\n\n---\n\n".join(results) or "No results found."
    except Exception as e: return f"DuckDuckGo Error: {str(e)}"