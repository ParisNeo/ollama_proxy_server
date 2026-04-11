TOOL_LIBRARY_NAME = 'ArXiv Explorer'
TOOL_LIBRARY_DESC = 'Search scientific papers and pre-prints on ArXiv.'
TOOL_LIBRARY_ICON = '🔬'

def init_tool_library() -> None:
    import pipmaster as pm
    pm.ensure_packages({'arxiv': '>=2.1.0'})

def tool_search_papers(args: dict):
    '''
    Search for scientific papers on ArXiv.
    
    Args:
        args: dict with keys:
            - query (str): Scientific keywords or paper ID
            - count (int, optional): Number of papers to fetch
    '''
    import arxiv
    try:
        client = arxiv.Client()
        search = arxiv.Search(query=args.get('query'), max_results=args.get('count', 3))
        results = []
        for res in client.results(search):
            results.append(f"[{res.entry_id}] {res.title}\nAbstract: {res.summary[:500]}...")
        return "\n\n".join(results) if results else "No papers found."
    except Exception as e:
        return f"Error: {str(e)}"
