language:python
TOOL_LIBRARY_NAME = 'Skill Architect Loader'
TOOL_LIBRARY_DESC = 'Dynamically search and load LoLLMs SKILL.md files into context.'
TOOL_LIBRARY_ICON = '📜'

def init_tool_library() -> None:
    import pipmaster as pm
    pm.ensure_packages(["numpy", "safe-store"])

async def tool_load_specialized_skill(args: dict, lollms=None):
    '''
    Search the Hub library for a specialized skill or workflow and retrieve its instructions.
    Use this when you encounter a task you don't have a specific protocol for.
    
    Args:
        args: dict with keys:
            - query (str): The functional requirement (e.g. 'code review', 'legal analysis')
            - search_mode (str, optional): 'vector' for semantic or 'keyword' for exact matching. Default 'vector'.
    '''
    from app.core.skills_manager import SkillsManager
    import asyncio
    
    query = args.get('query')
    mode = args.get('search_mode', 'vector')
    
    # We attempt to use the Hub's vectorizer if provided by the host interface
    # In the Hub, the vectorizer is accessible via app state which isn't directly 
    # in the tool process, so we use the SkillsManager's internal logic.
    
    # Note: LollmsSystem doesn't expose the vectorizer yet, so we fall back to keyword 
    # unless the tool initializes its own ST model (heavy).
    # REPAIR: We'll perform a direct search.
    
    results = await SkillsManager.search_skills(query, limit=1)
    
    if not results:
        return f"No specialized skills found for '{query}'."
    
    skill = results[0]
    return f"--- LOADED SKILL: {skill['name']} ---\n\n{skill['raw']}\n\n### INSTRUCTION: Incorporate the logic above into your next steps."