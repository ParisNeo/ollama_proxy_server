TOOL_LIBRARY_NAME = 'Memory Manager'
TOOL_LIBRARY_DESC = 'Save and retrieve persistent user information or facts.'
TOOL_LIBRARY_ICON = '🧠'

TOOL_TITLES = {
    "tool_save_memory": "💾 Storing User Preference",
    "tool_get_memory": "🔍 Recalling User Information"
}

def tool_save_memory(args: dict, lollms=None):
    '''
    Store a piece of information persistently for the user.
    Args:
        args: dict with keys:
            - key (str): The category or name of the memory
            - value (str): The information to remember
    '''
    if not lollms: return "Error: No host interface available."
    lollms.set(args['key'], args['value'], persistent=True)
    return f"Memory stored under key: {args['key']}"

def tool_get_memory(args: dict, lollms=None):
    '''
    Retrieve stored information by key.
    Args:
        args: dict with keys:
            - key (str): The memory key to retrieve
    '''
    if not lollms: return "Error: No host interface available."
    return lollms.get(args['key'], default="No memory found.")