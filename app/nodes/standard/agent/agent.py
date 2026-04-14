import json
import copy
from typing import Dict, Any
from app.nodes.base import BaseNode
from app.crud import server_crud
from app.core.events import event_manager, ProxyEvent

class AgentReasonerNode(BaseNode):
    node_type = "hub/agent"
    node_title = "Autonomous Agent"
    node_category = "Serving & Cognition"
    node_icon = "🧠"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        from app.core.memory_manager import CognitiveMemoryManager
        
        props = node.get("properties", {})
        msgs = await engine._resolve_input(node, 0) or engine.initial_messages
        scratchpad = copy.deepcopy(list(msgs))
        model = props.get("model", "auto")
        max_turns = int(props.get("max_turns", 10))
        
        # --- UNIVERSAL COGNITIVE MEMORY INJECTION ---
        if props.get("enable_memory"):
            # Identify user by the 'sender' field passed through the engine
            user_id = engine.sender or "anonymous"
            memory_context = await CognitiveMemoryManager.get_memory_context(engine.db, user_id, engine.name)
            
            # Inject memory context into the system prompt or as a high-priority user message
            if scratchpad and scratchpad[0].get("role") == "system":
                scratchpad[0]["content"] = f"{memory_context}\n\n{scratchpad[0]['content']}"
            else:
                scratchpad.insert(0, {"role": "system", "content": memory_context})

        tools = []
        for i in range(2, len(node.get("inputs", []))):
            t_data = await engine._resolve_input(node, i)
            if t_data:
                if isinstance(t_data, dict) and t_data.get("type") == "mcp_bundle":
                    # Extract tools from the MCP manifest
                    mcp_tools = t_data.get("tools", [])
                    for mt in mcp_tools:
                        mt["is_mcp"] = True
                        mt["mcp_client"] = t_data["client"]
                    tools.extend(mcp_tools)
                else:
                    tools.extend(t_data if isinstance(t_data, list) else [t_data])

        for turn in range(1, max_turns + 1):
            real_model, turn_msgs = await engine.resolve_target_fn(engine.db, model, scratchpad, engine.depth + 1, engine.request, engine.request_id, engine.sender)
            event_manager.emit(ProxyEvent("active", engine.request_id, f"Agent Turn {turn}", real_model, engine.sender, error_message=f"Thinking... ({turn}/{max_turns})"))
            
            cb = getattr(engine.request.state, "stream_callback", None)
            if cb:
                await cb(f'<processing type="tool_execution" title="Agent Loop" round="{turn}">\n* Agent is thinking (Turn {turn}/{max_turns})...\n')
            
            servers = await server_crud.get_servers_with_model(engine.db, real_model)
            if not servers:
                if cb: await cb(f'* Error: Server offline\n</processing>\n')
                break

            payload = {"model": real_model, "messages": turn_msgs, "stream": False, "tools": [t for t in tools if t]}
            resp, _ = await engine.reverse_proxy_fn(engine.request, "chat", servers, json.dumps(payload).encode(), is_subrequest=True, sender="autonomous-agent")
            
            if not hasattr(resp, 'body'):
                if cb: await cb(f'* Error: Empty response\n</processing>\n')
                break
                
            ai_msg = json.loads(resp.body.decode()).get("message", {})
            scratchpad.append(ai_msg)
            
            if cb:
                await cb(f'* Turn {turn} complete.\n</processing>\n')
            
            if not ai_msg.get("tool_calls"):
                content = ai_msg.get("content", "")
                
                # --- PROCESS MEMORY TAGS ---
                if props.get("enable_memory"):
                    # Extract tags, save to DB, and return clean text to user
                    content = await CognitiveMemoryManager.process_tags(engine.db, engine.sender, engine.name, content)
                
                return content if output_slot_idx == 0 else scratchpad
        return "Agent finished."