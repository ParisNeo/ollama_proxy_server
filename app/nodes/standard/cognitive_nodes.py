from typing import Dict, Any
from app.nodes.base import BaseNode
from app.database.models import MemoryEntry
from sqlalchemy import select

class MemoryBrowserNode(BaseNode):
    node_type = "hub/memory_browser"
    node_title = "Memory Browser"
    node_category = "Cognitive"
    node_icon = "🧠"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        category = await engine._resolve_input(node, 0) # Input: Category name
        if not category: return "No category specified."
        
        async with engine.db as db:
            res = await db.execute(
                select(MemoryEntry.title, MemoryEntry.content)
                .filter(MemoryEntry.category == category)
            )
            items = res.all()
            return "\n".join([f"- {i.title}: {i.content}" for i in items])