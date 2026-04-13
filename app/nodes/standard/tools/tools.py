from typing import Dict, Any
from app.nodes.base import BaseNode
from app.core.tools_manager import ToolsManager

class ToolSelectorNode(BaseNode):
    node_type = "hub/tool_selector"
    node_title = "Tool Selector"
    node_category = "Selectors"
    node_icon = "🛠️"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        lib_name = node["properties"].get("library")
        fn_name = node["properties"].get("function")
        if not lib_name or not fn_name: return None
        all_tools = ToolsManager.get_all_tools()
        lib = next((t for t in all_tools if t["filename"] == lib_name), None)
        if not lib: return None
        schemas = ToolsManager.get_tool_definitions(lib["raw"])
        if fn_name == "[ALL FUNCTIONS]":
            for s in schemas: s["library"] = lib_name
            return schemas
        for s in schemas:
            if s["function"]["name"] == fn_name:
                s["library"] = lib_name
                return s
        return None