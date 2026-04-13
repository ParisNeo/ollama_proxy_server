import re
import json
import asyncio
from typing import Dict, Any
from app.nodes.base import BaseNode

class AutoRouterNode(BaseNode):
    node_type = "hub/autorouter"
    node_title = "Auto Router"
    node_category = "Logic & Routing"
    node_icon = "🔀"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        history = await engine._resolve_input(node, 0) or []
        last_msg = history[-1] if history else {}
        user_text = (last_msg.get("content", "") if isinstance(last_msg.get("content"), str) else "").lower()
        
        selected_slot = 1 # Default fallback
        if node["inputs"][selected_slot].get("link"):
            return await engine.execute_cognitive_path(node["inputs"][selected_slot]["link"], history)
        return "Router selection failed."

class MOENode(BaseNode):
    node_type = "hub/moe"
    node_title = "Mixture of Experts"
    node_category = "Serving & Cognition"
    node_icon = "✨"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        import asyncio
        import json
        from app.core.events import event_manager, ProxyEvent
        from app.crud import server_crud

        props = node.get("properties", {})
        # Note: We must fetch experts first
        expert_tasks, expert_names = [], []
        for i in range(1, len(node.get("inputs", []))):
            inp = node["inputs"][i]
            if inp and inp.get("link"):
                link = engine.links.get(inp["link"])
                src_node = engine.nodes.get(link[1]) if link else None
                src_name = src_node.get("properties", {}).get("model", f"Expert {i}") if src_node else f"Expert {i}"
                expert_names.append(src_name)
                expert_tasks.append(engine.execute_cognitive_path(inp["link"], engine.initial_messages))
        
        if not expert_tasks: return "No experts connected."

        # Emit Status Telemetry
        status_lines = [f"- {name} is working" for name in expert_names]
        status_lines.append("- now the orchestrator is building final answer")
        processing_block = "<processing>\n" + "\n".join(status_lines) + "\n</processing>\n\n"
        
        # Execute Experts
        responses = await asyncio.gather(*expert_tasks, return_exceptions=True)
        
        # FIX: Ensure we handle exceptions or None results gracefully
        panel_data = ""
        for i, resp in enumerate(responses):
            name = expert_names[i]
            if isinstance(resp, Exception):
                val = f"Error: {str(resp)}"
            elif resp is None:
                val = "No response from expert."
            else:
                val = str(resp)
            panel_data += f"--- {name} ---\n{val}\n---   ---\n\n"

        # Synthesize
        orchestrator_target = props.get("orchestrator", "auto")
        real_orchestrator, _ = await engine.resolve_target_fn(
            engine.db, orchestrator_target, engine.initial_messages, engine.depth + 1, 
            engine.request, engine.request_id, engine.sender
        )
        
        sys_prompt = props.get("system_prompt", "Combine the ideas from the experts into a single high-quality response.")
        final_messages = list(engine.initial_messages) + [{"role": "user", "content": f"### EXPERT PANEL FEEDBACK:\n{panel_data}\n\n### MANDATE:\n{sys_prompt}"}]
        
        servers = await server_crud.get_servers_with_model(engine.db, real_orchestrator)
        if not servers: return f"[Error: Orchestrator model '{real_orchestrator}' offline]"

        resp, _ = await engine.reverse_proxy_fn(
            engine.request, "chat", servers, 
            json.dumps({"model": real_orchestrator, "messages": final_messages, "stream": False}).encode(), 
            is_subrequest=True, request_id=engine.request_id, model=real_orchestrator, sender=engine.sender
        )
        
        final_answer = "Error synthesis failed"
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            final_answer = data.get("message", {}).get("content", "Error: Empty response.")

        output = processing_block if props.get("send_status", True) else ""
        if props.get("show_intermediate", True):
            output += "## Intermediate Expert Insights\n\n" + panel_data + "\n\n"
            
        return output + "## Final Answer\n\n" + final_answer