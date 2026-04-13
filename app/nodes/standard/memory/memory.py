from typing import Dict, Any
from app.nodes.base import BaseNode

class MemoryLoaderNode(BaseNode):
    node_type = "hub/memory_loader"
    node_title = "Memory Loader"
    node_category = "Knowledge & RAG"
    node_icon = "🧠"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        key = await engine._resolve_input(node, 0) or node["properties"].get("key", "")
        if not key: return ""
        
        # Access persistent data via the engine's sender/user info
        from app.database.models import UserToolData
        from sqlalchemy import select
        
        async with engine.db as db:
            stmt = select(UserToolData.value).filter(
                UserToolData.key == key,
                UserToolData.library_name == "Memory Manager"
            )
            res = await db.execute(stmt)
            val = res.scalar()
            return str(val) if val else f"No memory stored for '{key}'"