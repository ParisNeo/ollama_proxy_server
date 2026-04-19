import json
import copy
from typing import Dict, Any
from app.nodes.base import BaseNode
from app.crud import server_crud
from app.core.tools_manager import ToolsManager
from app.core.events import event_manager, ProxyEvent
from fastapi.concurrency import run_in_threadpool
import logging
logger = logging.getLogger("Agent")

class AgentReasonerNode(BaseNode):
    node_type = "hub/agent"
    node_title = "Autonomous Agent"
    node_category = "Serving & Cognition"
    node_icon = "🧠"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        from app.core.memory_manager import CognitiveMemoryManager
        from app.core.skills_manager import SkillsManager
        from app.core.personalities_manager import PersonalityManager
        import datetime

        props = node.get("properties", {})
        msgs = await engine._resolve_input(node, 0) or engine.initial_messages
        input_settings = await engine._resolve_input(node, 1) or {}
        
        scratchpad = copy.deepcopy(list(msgs))
        final_settings = copy.deepcopy(input_settings)

        # 0. Handle Direct Persona Inheritance
        persona_name = props.get("persona")
        if persona_name:
            p = next((x for x in PersonalityManager.get_all_personalities() if x["name"] == persona_name), None)
            if p:
                # Extract prompt logic
                raw_text = re.sub(r'^---\n.*?\n---\n', '', p["raw"], flags=re.DOTALL).strip()
                raw_text = raw_text.replace("{{user_name}}", engine.sender).replace("{{display_name}}", persona_name)
                now = datetime.datetime.now()
                raw_text = raw_text.replace("{{date}}", now.strftime("%Y-%m-%d")).replace("{{time}}", now.strftime("%H:%M:%S"))

                # Inject System Prompt
                if scratchpad and scratchpad[0].get("role") == "system":
                    scratchpad[0]["content"] = f"{raw_text}\n\n{scratchpad[0]['content']}"
                else:
                    scratchpad.insert(0, {"role": "system", "content": raw_text})
                
                # Inherit Settings (Metadata)
                meta = SkillsManager.parse_frontmatter(p["raw"])
                # Merge: Input Slot < Persona Metadata < Node Defaults
                if "model_hints" in meta and isinstance(meta["model_hints"], dict):
                    hints = meta["model_hints"]
                    if "temperature" in hints and "temperature" not in final_settings:
                        final_settings["temperature"] = float(hints["temperature"])
                    if "max_tokens" in hints and "num_predict" not in final_settings:
                        final_settings["num_predict"] = int(hints["max_tokens"])
        # 1. Resolve Model (Property < Priority Input)
        model_override = await engine._resolve_input(node, 2)
        model = str(model_override).strip() if model_override else props.get("model", "auto")
        max_turns = int(props.get("max_turns", 10))
        
        # --- UNIVERSAL COGNITIVE MEMORY INJECTION ---
        memory_system = props.get("memory_system", "none")
        if memory_system and memory_system != "none":
            # Identify user by the 'sender' field passed through the engine
            user_id = engine.sender or "anonymous"
            memory_context = await CognitiveMemoryManager.get_memory_context(engine.db, user_id, memory_system)
            
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
            
            candidates = real_model if isinstance(real_model, list) else [real_model]
            resp = None
            ai_msg = {}
            error_msg = ""
            
            cb = getattr(engine.request.state, "stream_callback", None)
            if cb:
                await cb(f'<processing type="tool_execution" title="Agent Loop" round="{turn}">\n')

            for rm in candidates:
                rm_str = str(rm)
                event_manager.emit(ProxyEvent("active", engine.request_id, f"Agent Turn {turn}", rm_str, engine.sender, error_message=f"Thinking... ({turn}/{max_turns})"))
                
                servers = await server_crud.get_servers_with_model(engine.db, rm_str)
                if not servers:
                    error_msg = f"Compute nodes for '{rm_str}' offline."
                    logger.warning(f"Agent candidate '{rm_str}' offline. Falling back...")
                    continue

                payload = {
                    "model": rm_str, 
                    "messages": turn_msgs, 
                    "stream": False, 
                    "tools":[t for t in tools if t],
                    "options": final_settings # Pass inherited settings
                }
                try:
                    resp, _ = await engine.reverse_proxy_fn(engine.request, "chat", servers, json.dumps(payload).encode(), is_subrequest=True, sender="autonomous-agent")
                    if hasattr(resp, 'body'):
                        data = json.loads(resp.body.decode())
                        ai_msg = data.get("message", {})
                        if ai_msg:
                            logger.info(f"Agent successfully used candidate '{rm_str}'.")
                            break # Success!
                except Exception as e:
                    error_msg = str(e)
                    logger.warning(f"Agent candidate '{rm_str}' failed: {e}. Falling back...")
                    continue
            
            if not ai_msg:
                if cb: await cb(f'* Error: All candidates failed. Last error: {error_msg}\n</processing>\n')
                return f"❌ Agent Fallback Exhausted: All candidates failed. Last error: {error_msg}"

            scratchpad.append(ai_msg)
            
            if cb:
                await cb('</processing>\n')
            
            if ai_msg.get("tool_calls"):
                # --- PRETTY TOOL TELEMETRY (LTP Protocol) ---
                if cb:
                    for call in ai_msg["tool_calls"]:
                        fn_name = call.get("function", {}).get("name", "unknown")
                        args = call.get("function", {}).get("arguments", {})
                        if isinstance(args, str):
                            try: args = json.loads(args)
                            except: pass
                        
                        tool_def = next((t for t in tools if t.get("function", {}).get("name") == fn_name), {})
                        pretty_text = tool_def.get("pretty_name")
                        
                        if not pretty_text:
                            pretty_text = "🛠️ " + fn_name.replace("tool_", "").replace("_", " ").title()
                        
                        preview = str(args.get('query') or args.get('path') or args.get('command') or args.get('url') or "")
                        if len(preview) > 60: preview = preview[:57] + "..."
                        
                        await cb(f'{pretty_text}: "{preview}"\n')

                # --- LTP TOOL EXECUTION LOOP ---
                for call in ai_msg["tool_calls"]:
                    fn_name = call.get("function", {}).get("name")
                    args = call.get("function", {}).get("arguments", {})
                    if isinstance(args, str):
                        try: args = json.loads(args)
                        except: args = {}

                    tool_def = next((t for t in tools if t.get("function", {}).get("name") == fn_name), None)
                    if not tool_def:
                        scratchpad.append({"role": "tool", "tool_call_id": call.get("id"), "name": fn_name, "content": "Error: Tool schema not found in this node's context."})
                        continue

                    try:
                        if tool_def.get("is_mcp"):
                            result = await tool_def["mcp_client"].call_tool(fn_name, args)
                        else:
                            from app.api.v1.routes.tools import execute_tool_universally
                            lib_name = tool_def.get("library")
                            all_libs = ToolsManager.get_all_tools()
                            lib = next((l for l in all_libs if l["filename"] == lib_name), None)
                            
                            if lib:
                                # Use current user context if available, otherwise anonymous system user
                                active_user = getattr(engine.request.state, 'user', None)
                                if not active_user:
                                    from app.database.models import User as DBUser
                                    active_user = DBUser(id=0, username="system_anon", is_admin=True)

                                result, logs = await run_in_threadpool(execute_tool_universally, lib["raw"], fn_name, args, active_user, lib_name)
                            else:
                                result = f"Error: Tool library '{lib_name}' not found on disk."
                        
                        scratchpad.append({"role": "tool", "tool_call_id": call.get("id"), "name": fn_name, "content": str(result)})
                    except Exception as e:
                        scratchpad.append({"role": "tool", "tool_call_id": call.get("id"), "name": fn_name, "content": f"Error: {str(e)}"})
                
                continue # RE-RUN AI LOOP WITH RESULTS

            if not ai_msg.get("tool_calls"):
                content = ai_msg.get("content", "")
                
                # --- PROCESS MEMORY TAGS ---
                if memory_system and memory_system != "none":
                    # Extract tags, save to DB, and return clean text to user
                    content = await CognitiveMemoryManager.process_tags(engine.db, engine.sender, memory_system, content)
                
                # REPAIR MISSION: Ensure we emit a final completion trace before returning
                if cb:
                    await cb(f'* Task finalized by {rm_str}.\n')
                
                return content if output_slot_idx == 0 else scratchpad
        return "Agent finished."