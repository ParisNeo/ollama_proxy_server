import json
from typing import Dict, Any
from app.nodes.base import BaseNode
from app.crud import server_crud

class LLMChatNode(BaseNode):
    node_type = "hub/llm_chat"
    node_title = "LLM Chat"
    node_category = "Serving & Cognition"
    node_icon = "💬"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        target_model = str(node["properties"].get("model", "auto")).strip()
        msgs = await engine._resolve_input(node, 0) or engine.initial_messages
        settings = await engine._resolve_input(node, 1) or {}
        
        tools = []
        for i in range(3, len(node.get("inputs", []))):
            t = await engine._resolve_input(node, i)
            if t: tools.extend(t if isinstance(t, list) else [t])
        
        real_model, final_msgs = await engine.resolve_target_fn(
            engine.db, target_model, msgs, engine.depth + 1, engine.request, engine.request_id, engine.sender
        )
        
        payload = {"model": real_model, "messages": final_msgs, "stream": False, "options": settings}
        if tools: payload["tools"] = [t for t in tools if t]

        servers = await server_crud.get_servers_with_model(engine.db, real_model)
        if not servers: return f"[Error: Model {real_model} offline]"

        resp, _ = await engine.reverse_proxy_fn(engine.request, "chat", servers, json.dumps(payload).encode(), is_subrequest=True, request_id=engine.request_id, model=real_model, sender=engine.sender)
        
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            return data.get("message", {}).get("content", "")
        return "[Error: Empty response]"