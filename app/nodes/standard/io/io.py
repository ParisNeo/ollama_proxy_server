from typing import Dict, Any
from app.nodes.base import BaseNode

class InputNode(BaseNode):
    node_type = "hub/input"
    node_title = "Request Input"
    node_category = "IO & Terminal"
    node_icon = "📥"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        if output_slot_idx == 0: return engine.initial_messages
        if output_slot_idx == 1: return {}
        if output_slot_idx == 2: 
            if not engine.initial_messages: return ""
            last_msg = engine.initial_messages[-1]
            content = last_msg.get("content", "")
            return content if isinstance(content, str) else ""
        return None

class OutputNode(BaseNode):
    node_type = "hub/output"
    node_title = "Global Output"
    node_category = "IO & Terminal"
    node_icon = "📤"
    
    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        return await engine._resolve_input(node, 0)