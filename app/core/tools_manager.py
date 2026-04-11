import os
import re
import io
import ast
from pathlib import Path
from typing import List, Dict, Any

SYSTEM_TOOLS_DIR = Path("app/tools")
USER_TOOLS_DIR = Path.home() / ".lollms_hub" / "tools"

class ToolsManager:
    @staticmethod
    def ensure_dirs():
        SYSTEM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        USER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

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
        except Exception:
            pass
        return meta

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