import os
import re
import io
import ast
from pathlib import Path
from typing import List, Dict, Any

SYSTEM_TOOLS_DIR = Path("app/tools")
USER_TOOLS_DIR = Path.home() / ".lollms_hub" / "tools"

BOOTSTRAP_TOOLS = [
    {
        "name": "wikipedia_search.py",
        "content": """TOOL_LIBRARY_NAME = 'Wikipedia Search'
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
                output.append(f"--- {title} ---\\n{page}")
            except: continue
        return "\\n\\n".join(output) if output else "No results found."
    except Exception as e:
        return f"Error: {str(e)}"
"""
    },
    {
        "name": "arxiv_search.py",
        "content": """TOOL_LIBRARY_NAME = 'ArXiv Explorer'
TOOL_LIBRARY_DESC = 'Search scientific papers and pre-prints on ArXiv.'
TOOL_LIBRARY_ICON = '🔬'

def init_tool_library() -> None:
    import pipmaster as pm
    pm.ensure_packages({'arxiv': '>=2.1.0'})

def tool_search_papers(args: dict):
    '''
    Search for scientific papers on ArXiv with optional date filtering.
    
    Args:
        args: dict with keys:
            - query (str): Scientific keywords or topics
            - count (int, optional): Number of papers to fetch (default: 3)
            - year_start (int, optional): Start year for filtering (inclusive)
            - year_end (int, optional): End year for filtering (inclusive)
    '''
    import arxiv
    try:
        query = args.get('query', '')
        # Build ArXiv advanced query syntax if years are provided
        if args.get('year_start') or args.get('year_end'):
            start = args.get('year_start', 1800)
            end = args.get('year_end', 2100)
            # ArXiv uses YYYYMMDDHHMMSS format for dates
            query = f"({query}) AND submittedDate:[{start}01010000 TO {end}12312359]"

        client = arxiv.Client()
        search = arxiv.Search(
            query=query, 
            max_results=args.get('count', 3),
            sort_by=arxiv.SortCriterion.Relevance
        )
        
        results = []
        for res in client.results(search):
            date_str = res.published.strftime('%Y-%m-%d')
            results.append(f"--- {res.title} ({date_str}) ---\\nURL: {res.entry_id}\\nAbstract: {res.summary[:500]}...")
        
        return "\\n\\n".join(results) if results else f"No papers found for: {query}"
    except Exception as e:
        return f"ArXiv Error: {str(e)}"
"""
    }
]

class ToolsManager:
    @staticmethod
    def ensure_dirs():
        SYSTEM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        USER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        # Bootstrap default tools if directory is empty
        for tool in BOOTSTRAP_TOOLS:
            target = SYSTEM_TOOLS_DIR / tool["name"]
            if not target.exists():
                target.write_text(tool["content"], encoding="utf-8")

    @staticmethod
    def parse_metadata(content: str) -> Dict[str, str]:
        """Extracts global variables from Python source using AST for safety."""
        meta = {
            "name": "Unnamed Tool Library",
            "description": "No description provided.",
            "icon": "🔧"
        }
        try:
            tree = ast.parse(content)
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            if target.id == "TOOL_LIBRARY_NAME":
                                meta["name"] = ast.literal_eval(node.value)
                            elif target.id == "TOOL_LIBRARY_DESC":
                                meta["description"] = ast.literal_eval(node.value)
                            elif target.id == "TOOL_LIBRARY_ICON":
                                meta["icon"] = ast.literal_eval(node.value)
                            elif target.id == "TOOL_SETTINGS_METADATA":
                                meta["settings_metadata"] = ast.literal_eval(node.value)
        except Exception:
            pass
        return meta

    @staticmethod
    @staticmethod
    def get_tool_definitions(content: str) -> List[Dict[str, Any]]:
        """Parses docstrings using AST to build OpenAI-compatible tool definitions."""
        tools = []
        try:
            tree = ast.parse(content)
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name.startswith("tool_"):
                    docstring = ast.get_docstring(node) or "No description provided."
                    
                    params = {"type": "object", "properties": {}, "required": []}
                    
                    # ENHANCED PARSER: Handle arbitrary indentation and 'optional' flags
                    # Matches: [any space] - [name] ([type], [optional]): [desc]
                    arg_matches = re.finditer(r'^\s*-\s+([\w_]+)\s*\(([\w_]+)(?:,\s*optional)?\):\s*(.*)', docstring, re.MULTILINE | re.IGNORECASE)
                    
                    for m in arg_matches:
                        name, p_type, desc = m.groups()
                        p_type_map = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "dict": "object", "list": "array"}
                        
                        params["properties"][name] = {
                            "type": p_type_map.get(p_type.lower(), "string"),
                            "description": desc.strip()
                        }
                        # If the docstring explicitly says "required" or doesn't mention "optional", add to required list
                        if "optional" not in m.group(0).lower():
                            params["required"].append(name)

                    # If no specific args found, assume a generic 'query' for compatibility
                    if not params["properties"]:
                        params["properties"]["args"] = {"type": "object", "description": "Arguments for the tool"}

                    tools.append({
                        "type": "function",
                        "function": {
                            "name": node.name,
                            "description": docstring.split('\n\n')[0].strip(),
                            "parameters": params
                        }
                    })
        except Exception: pass
        return tools

    @staticmethod
    def get_all_tools() -> List[Dict[str, Any]]:
        ToolsManager.ensure_dirs()
        tools_map = {}

        def _scan_dir(directory):
            for file_path in directory.glob("*.py"):
                if file_path.name == "__init__.py": continue
                try:
                    content = file_path.read_text(encoding="utf-8")
                    meta = ToolsManager.parse_metadata(content)
                    tools_map[file_path.name] = {
                        "filename": file_path.name,
                        "name": meta["name"],
                        "description": meta["description"],
                        "icon": meta["icon"],
                        "raw": content
                    }
                except Exception: pass

        _scan_dir(SYSTEM_TOOLS_DIR)
        _scan_dir(USER_TOOLS_DIR)
        
        return sorted(list(tools_map.values()), key=lambda x: x["name"].lower())

    @staticmethod
    def save_tool(filename: str, content: str) -> str:
        ToolsManager.ensure_dirs()
        safe_filename = re.sub(r'[^\w\-\.]', '', filename)
        if not safe_filename.endswith(".py"):
            safe_filename += ".py"
        (USER_TOOLS_DIR / safe_filename).write_text(content, encoding="utf-8")
        return safe_filename

    @staticmethod
    def delete_tool(filename: str) -> bool:
        safe_filename = re.sub(r'[^\w\-\.]', '', filename)
        file_path = USER_TOOLS_DIR / safe_filename
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            return True
        return False