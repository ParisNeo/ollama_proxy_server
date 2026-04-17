import re
import json
import asyncio
from typing import Dict, Any
from app.nodes.base import BaseNode

class AutoRouterNode(BaseNode):
    node_type = "hub/autorouter"
    node_title = "Smart Router"
    node_category = "Logic & Routing"
    node_icon = "🚦"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        msgs = await engine._resolve_input(node, 0) or engine.initial_messages
        props = node.get("properties", {})
        mode = props.get("mode", "rules")
        
        if not msgs: return props.get("default_model", "auto")
        
        last_msg = msgs[-1]
        text = last_msg.get("content", "")
        if isinstance(text, list):
            text = " ".join([p.get("text", "") for p in text if p.get("type") == "text"])
        
        # --- MODE 1: Deterministic Firewall Rules ---
        if mode == "rules":
            for rule in props.get("rules", []):
                r_type = rule.get("type")
                r_val = rule.get("value")
                target = rule.get("target")
                
                match = False
                if r_type == "keyword": match = str(r_val).lower() in text.lower()
                elif r_type == "regex": match = bool(re.search(str(r_val), text, re.I))
                elif r_type == "min_len": match = len(text) >= int(r_val or 0)
                elif r_type == "max_len": match = len(text) <= int(r_val or 0)
                
                if match:
                    logger.info(f"Router Firewall Match: {r_type}='{r_val}' -> {target}")
                    return target

        # --- MODE 2: Semantic LLM Classifier ---
        else:
            classifier = props.get("classifier_model", "auto")
            pool = props.get("candidate_models", ["auto"])
            
            prompt = (
                f"Analyze the following user message. Based on the intent, which model from this list is best suited to handle it?\n"
                f"MODELS: {', '.join(pool)}\n\n"
                f"USER MESSAGE: \"{text[:500]}\"\n\n"
                f"STRICT: Output ONLY the name of the chosen model. No explanation."
            )
            
            try:
                from app.crud import server_crud
                import json
                
                servers = await server_crud.get_servers_with_model(engine.db, classifier)
                if servers:
                    payload = {"model": classifier, "messages": [{"role": "user", "content": prompt}], "stream": False}
                    resp, _ = await engine.reverse_proxy_fn(engine.request, "chat", servers, json.dumps(payload).encode(), is_subrequest=True, sender="router-classifier")
                    
                    if hasattr(resp, 'body'):
                        data = json.loads(resp.body.decode())
                        choice = data.get("message", {}).get("content", "").strip()
                        # Verify the AI didn't hallucinate a name not in the pool
                        if choice in pool:
                            logger.info(f"Router Semantic Match: Classifier chose {choice}")
                            return choice
            except Exception as e:
                logger.error(f"Router Classifier Error: {e}")

        # Final Fallback
        return props.get("default_model", "auto")

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

        # Emit Status Telemetry via stream_callback if available
        status_lines =[f"  - polling {name}..." for name in expert_names]
        cb = getattr(engine.request.state, "stream_callback", None)
        if cb:
            # UI FIX: Just append lines to the engine's existing block
            await cb(f'* MIXTURE OF EXPERTS: Activating {len(expert_names)} models in parallel...\n' + 
                     "\n".join(status_lines) + '\n')
        
        processing_block = "" # Legacy text injection disabled in favor of Lollms Processing Protocol
        
        # Execute Experts
        responses = await asyncio.gather(*expert_tasks, return_exceptions=True)
        if cb:
            await cb(f'* All {len(responses)} experts replied. Synthesizing final answer...\n</processing>\n')
        
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
        final_messages = list(engine.initial_messages) +[{"role": "user", "content": f"### EXPERT PANEL FEEDBACK:\n{panel_data}\n\n### MANDATE:\n{sys_prompt}"}]
        
        candidates = real_orchestrator if isinstance(real_orchestrator, list) else [real_orchestrator]
        final_answer = "Error synthesis failed"
        
        for rm in candidates:
            rm_str = str(rm)
            servers = await server_crud.get_servers_with_model(engine.db, rm_str)
            if not servers: continue

            try:
                resp, _ = await engine.reverse_proxy_fn(
                    engine.request, "chat", servers, 
                    json.dumps({"model": rm_str, "messages": final_messages, "stream": False}).encode(), 
                    is_subrequest=True, request_id=engine.request_id, model=rm_str, sender=engine.sender
                )
                if hasattr(resp, 'body'):
                    data = json.loads(resp.body.decode())
                    if data.get("message", {}).get("content"):
                        final_answer = data["message"]["content"]
                        break
            except Exception:
                continue

        output = processing_block if props.get("send_status", True) else ""
        if props.get("show_intermediate", True):
            output += "## Intermediate Expert Insights\n\n" + panel_data + "\n\n"
            
        return output + "## Final Answer\n\n" + final_answer