from typing import Dict, Any
from app.nodes.base import BaseNode

class NoteNode(BaseNode):
    node_type = "hub/note"
    node_title = "Markdown Note"
    node_category = "Utility"
    node_icon = "📝"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        return node["properties"].get("content", "")