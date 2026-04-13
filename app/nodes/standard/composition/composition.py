import copy
from typing import Dict, Any
from app.nodes.base import BaseNode

class SystemModifierNode(BaseNode):
    node_type = "hub/system_modifier"
    node_title = "System Modifier"
    node_category = "Logic & Routing"
    node_icon = "⚡"    
    
    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        history = await engine._resolve_input(node, 0) or engine.initial_messages
        sys_prompt = await engine._resolve_input(node, 1)
        if not sys_prompt: return history
        
        updated = copy.deepcopy(history)
        if node["properties"].get("replace_all", False):
            updated = [m for m in updated if m.get("role") != "system"]
            updated.insert(0, {"role": "system", "content": str(sys_prompt)})
        else:
            system_msg = next((m for m in updated if m.get("role") == "system"), None)
            if system_msg: system_msg["content"] = f"{system_msg['content']}\n\n{sys_prompt}".strip()
            else: updated.insert(0, {"role": "system", "content": str(sys_prompt)})
        return updated

class SystemComposerNode(BaseNode):
    node_type = "hub/system_composer"
    node_title = "System Composer"
    node_category = "Logic & Routing"
    node_icon = "🏗️"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        parts = []
        for i in range(len(node.get("inputs", []))):
            val = await engine._resolve_input(node, i)
            if val: parts.append(str(val))
        return "\n\n".join(parts)