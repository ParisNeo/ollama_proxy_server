TOOL_LIBRARY_NAME = 'Wikipedia Search'
TOOL_LIBRARY_DESC = 'Search and retrieve article summaries from Wikipedia.'
TOOL_LIBRARY_ICON = '📖'

def init_tool_library() -> None:
    '''Initialize dependencies using pipmaster'''
    import pipmaster as pm
    pm.ensure_packages({'wikipedia': '>=1.4.0'})

def tool_search_wikipedia(args: dict):
    '''
    Search Wikipedia for articles matching a query and return summaries.
    
    Args:
        args: dict with keys:
            - query (str): The search term or phrase
            - max_results (int, optional): Maximum number of results to return (default: 3)
    '''
    import wikipedia
    try:
        query = args.get('query')
        limit = args.get('max_results', 3)
        search_results = wikipedia.search(query)
        output = []
        for title in search_results[:limit]:
            try:
                page = wikipedia.summary(title, sentences=5)
                output.append(f"--- {title} ---\n{page}")
            except: continue
        return "\n\n".join(output) if output else "No results found."
    except Exception as e:
        return f"Error: {str(e)}"
