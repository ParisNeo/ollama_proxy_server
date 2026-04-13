import json
from typing import Dict, Any, List
from app.nodes.base import BaseNode

class ForLoopNode(BaseNode):
    node_type = "hub/for_loop"
    node_title = "For Loop"
    node_category = "Iteration"
    node_icon = "🔄"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        input_data = await engine._resolve_input(node, 0)
        
        # Convert input to a list we can iterate
        items = []
        if isinstance(input_data, list):
            items = input_data
        elif isinstance(input_data, str):
            # Try to parse JSON list, else split by lines
            try: items = json.loads(input_data)
            except: items = input_data.split('\n')
        
        # LiteGraph execution in our engine is pull-based.
        # To handle a loop, we evaluate the "Body" branch for every item 
        # and collect results.
        body_link = node["outputs"][0].get("links") # Slot 0 is 'Item'
        
        results = []
        if body_link:
            for i, item in enumerate(items):
                # We override the memoization for the loop body to allow re-execution
                # This is a simplified approach for the current engine
                res = await engine.execute_cognitive_path(body_link[0], [{"role": "user", "content": str(item)}])
                results.append(res)

        if output_slot_idx == 1: # Slot 1 is 'Combined Results'
            return results
        return results[-1] if results else ""

class WhileLoopNode(BaseNode):
    node_type = "hub/while_loop"
    node_title = "While Loop"
    node_category = "Iteration"
    node_icon = "♾️"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        max_iters = node["properties"].get("max_iterations", 5)
        current_data = await engine._resolve_input(node, 0)
        
        for _ in range(max_iters):
            # Check condition slot (Slot 1)
            should_continue = await engine._resolve_input(node, 1)
            if not should_continue or str(should_continue).lower() in ('false', 'no', 'stop'):
                break
                
            # Execute Loop Body (Slot 0 output)
            body_links = node["outputs"][0].get("links")
            if body_links:
                current_data = await engine.execute_cognitive_path(body_links[0], current_data)
            else:
                break
                
        return current_data