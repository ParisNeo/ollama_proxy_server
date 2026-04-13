import re
from typing import Dict, Any
from app.nodes.base import BaseNode

class ExtractTextNode(BaseNode):
    node_type = "hub/extract_text"
    node_title = "Extract Text"
    node_category = "Logic & Routing"
    node_icon = "📝"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        msgs = await engine._resolve_input(node, 0)
        if msgs and isinstance(msgs, list):
            last_msg = msgs[-1]
            content = last_msg.get("content", "")
            if isinstance(content, list):
                return "\n".join([p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]).strip()
            return content
        return ""