language:python
TOOL_LIBRARY_NAME = 'Wikipedia Encyclopedia'
TOOL_LIBRARY_DESC = 'Retrieve deep factual summaries from Wikipedia.'
TOOL_LIBRARY_ICON = '📚'

TOOL_TITLES = {
    "tool_search_wikipedia": "📖 consulting Wikipedia"
}

def init_tool_library() -> None:
    import pipmaster as pm
    pm.ensure_packages({'wikipedia': '>=1.4.0'})

def tool_search_wikipedia(args: dict, lollms=None):
    '''
    Search Wikipedia for verified information.
    
    Args:
        args: dict with keys:
            - query (str): The search term.
    '''
    import wikipedia
    try:
        query = args.get('query')
        # We try to get a direct summary
        try:
            return wikipedia.summary(query, sentences=10)
        except wikipedia.DisambiguationError as e:
            # If ambiguous, pick the first option
            return wikipedia.summary(e.options[0], sentences=10)
        except wikipedia.PageError:
            return f"Error: No Wikipedia page found for '{query}'."
    except Exception as e:
        return f"Error: {str(e)}"