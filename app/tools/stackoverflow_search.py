TOOL_LIBRARY_NAME = 'StackOverflow Search'
TOOL_LIBRARY_DESC = 'Search StackOverflow for programming answers and solutions.'
TOOL_LIBRARY_ICON = '🥞'

def init_tool_library() -> None:
    import pipmaster as pm
    pm.ensure_packages({'requests': '>=2.0.0'})

def tool_search_stackoverflow(args: dict, lollms=None):
    '''
    Search StackOverflow for answers to programming questions.
    
    Args:
        args: dict with keys:
            - query (str): The programming question or error message to search for.
            - max_results (int, optional): The maximum number of results to return (default: 3).
    '''
    import requests
    
    try:
        query = args.get('query')
        limit = args.get('max_results', 3)
        
        if not query:
            return "Error: No search query provided."
            
        url = "https://api.stackexchange.com/2.3/search/advanced"
        params = {
            "order": "desc",
            "sort": "relevance",
            "q": query,
            "site": "stackoverflow",
            "filter": "withbody", # Include question body to give the AI context
            "pagesize": limit
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        items = data.get("items",[])
        if not items:
            return "No results found on StackOverflow for this query."
            
        results =[]
        for item in items:
            title = item.get("title", "")
            link = item.get("link", "")
            body = item.get("body_markdown", item.get("body", ""))[:500] # Truncate body
            results.append(f"Q: {title}\nURL: {link}\nPreview: {body}...")
            
        return "\n\n---\n\n".join(results)
    except Exception as e:
        return f"Execution Error: {str(e)}"