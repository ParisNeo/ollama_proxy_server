TOOL_LIBRARY_NAME = 'Standard Web Search'
TOOL_LIBRARY_DESC = 'Search the internet for real-time information using DuckDuckGo or Brave.'
TOOL_LIBRARY_ICON = '🌐'

# Define settings the user must configure in the Hub UI
TOOL_SETTINGS_METADATA = [
    {"name": "brave_api_key", "type": "password", "description": "Optional: Use Brave Search for higher quality results."},
    {"name": "max_results", "type": "number", "default": 5, "description": "Number of snippets to return."}
]

def init_tool_library() -> None:
    import pipmaster as pm
    pm.ensure_packages({'duckduckgo-search': '>=6.0.0', 'httpx': '>=0.27.0'})

def tool_web_search(args: dict, lollms=None):
    '''
    Search the web for a query.
    
    Args:
        args: dict with keys:
            - query (str): The search phrase.
    '''
    # Credentials are automatically injected by the Hub into the 'lollms' object
    brave_key = lollms.get_setting('brave_api_key') if lollms else None
    limit = int(lollms.get_setting('max_results', 5))
    query = args.get('query')

    if brave_key:
        # High-quality Brave path
        import httpx
        try:
            headers = {"Accept": "application/json", "X-Subscription-Token": brave_key}
            resp = httpx.get(f"https://api.search.brave.com/res/v1/web/search?q={query}", headers=headers, timeout=10)
            data = resp.json()
            results = [f"[{r['title']}]({r['url']})\n{r['description']}" for r in data.get('web', {}).get('results', [])[:limit]]
            return "\n\n".join(results) or "No results found."
        except Exception as e:
            return f"Brave Error: {str(e)}"

    # Free DuckDuckGo path
    from duckduckgo_search import DDGS
    try:
        with DDGS() as ddgs:
            results = [f"{r['title']}\nURL: {r['href']}\nSnippet: {r['body']}" for r in ddgs.text(query, max_results=limit)]
            return "\n\n---\n\n".join(results) if results else "No results found."
    except Exception as e:
        return f"Search Error: {str(e)}"