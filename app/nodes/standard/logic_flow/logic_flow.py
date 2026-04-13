import re
from typing import Dict, Any
from app.nodes.base import BaseNode

class IfElseNode(BaseNode):
    node_type = "hub/if_else"
    node_title = "If / Else"
    node_category = "Logic & Routing"
    node_icon = "⚖️"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        value = await engine._resolve_input(node, 0)
        condition = str(node["properties"].get("condition", ""))
        mode = node["properties"].get("mode", "contains")
        
        # Determine Truthiness
        is_true = False
        val_str = str(value).lower()
        cond_str = condition.lower()

        if mode == "contains": is_true = cond_str in val_str
        elif mode == "equals": is_true = cond_str == val_str
        elif mode == "regex": 
            try: is_true = bool(re.search(condition, str(value), re.I))
            except: is_true = False
        elif mode == "exists": is_true = bool(value)

        # Branching logic
        if is_true:
            return value if output_slot_idx == 0 else None
        else:
            return value if output_slot_idx == 1 else None

class SwitchCaseNode(BaseNode):
    node_type = "hub/switch_case"
    node_title = "Switch / Case"
    node_category = "Logic & Routing"
    node_icon = "⑂"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        value = await engine._resolve_input(node, 0)
        val_str = str(value).strip()
        
        cases = node["properties"].get("cases", [])
        
        # Check if the requested output slot matches the case for the input value
        # output_slot_idx matches the index in the 'cases' list
        if output_slot_idx < len(cases):
            target_case = str(cases[output_slot_idx]).strip()
            if val_str == target_case:
                return value
        
        # Last slot is always 'Default'
        if output_slot_idx == len(cases):
            # If no other case matched, return to default
            any_match = any(str(c).strip() == val_str for c in cases)
            return value if not any_match else None
            
        return None