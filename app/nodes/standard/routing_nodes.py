import re
import json
import asyncio
from typing import Dict, Any
from app.nodes.base import BaseNode
from app.core.events import event_manager, ProxyEvent
from app.crud import server_crud

class AutoRouterNode(BaseNode):
    node_type = "hub/autorouter"
    node_title = "Auto Router"
    node_category = "Logic & Routing"
    node_icon = "🔀"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeAutoRouter() {
    this.addInput("User Context", "messages");
    this.addInput("Expert 1", "expert,string");
    this.addOutput("Route Output", "string");
    this.title = "🔀 AUTO ROUTER";
    this.addWidget("button", "Configure Rules", null, () => { window.openRouterModal(this); });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/autorouter", NodeAutoRouter);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        props = node.get("properties", {})
        history = await engine._resolve_input(node, 0) or []
        last_msg = history[-1] if history else {}
        user_text = (last_msg.get("content", "") if isinstance(last_msg.get("content"), str) else "").lower()
        
        has_images = False
        if isinstance(last_msg.get("content"), list):
            has_images = any(p.get("type") == "image_url" for p in last_msg["content"])
        elif last_msg.get("images"):
            has_images = True

        selected_slot = -1
        
        # 1. Evaluate Logical Rules
        for rule in props.get("rules", []):
            conditions = rule.get("conditions", [])
            if not conditions: continue
            
            matches = []
            for cond in conditions:
                c_type = cond.get("type")
                c_val = str(cond.get("value", ""))
                c_match = False

                if c_type == "has_images": c_match = has_images
                elif c_type == "min_len": c_match = len(user_text) >= int(c_val or 0)
                elif c_type == "max_len": c_match = len(user_text) <= int(c_val or 0)
                elif c_type == "keyword": c_match = c_val.lower() in user_text
                elif c_type == "regex":
                    try: c_match = bool(re.search(c_val, user_text, re.I))
                    except: pass
                elif c_type == "user": c_match = engine.sender == c_val
                
                matches.append(c_match)
            
            if matches and all(matches):
                selected_slot = rule["slot"]
                break

        # 2. Semantic Path Fallback
        if selected_slot == -1 and props.get("use_semantic") and props.get("rules"):
            c_model = props.get("classifier_model", "auto")
            intents_map = {r["slot"]: r["intent"] for r in props["rules"] if r["intent"]}
            if intents_map:
                prompt = f"Classify this: '{user_text}'\\nPaths:\\n" + "\\n".join([f"- PATH {s}: {d}" for s, d in intents_map.items()]) + "\\nOutput ONLY 'PATH X'."
                event_manager.emit(ProxyEvent("active", engine.request_id, "Auto Router", c_model, engine.sender, error_message="Classifying intent..."))
                servers = await server_crud.get_servers_with_model(engine.db, c_model)
                if servers:
                    resp, _ = await engine.reverse_proxy_fn(engine.request, "chat", servers, json.dumps({"model": c_model, "messages": [{"role": "user", "content": prompt}], "stream": False}).encode(), is_subrequest=True)
                    if hasattr(resp, 'body'):
                        match = re.search(r'PATH (\d+)', json.loads(resp.body.decode()).get("message", {}).get("content", "").upper())
                        if match: selected_slot = int(match.group(1))

        if selected_slot == -1 or selected_slot >= len(node.get("inputs", [])): selected_slot = 1
        
        # 3. Follow the chosen path
        if node["inputs"][selected_slot].get("link"):
            return await engine.execute_cognitive_path(node["inputs"][selected_slot]["link"], history)
        return "Router picked empty slot."

class MessageSelectorNode(BaseNode):
    node_type = "hub/selector"
    node_title = "Message Selector"
    node_category = "Logic & Routing"
    node_icon = "⚖️"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeSelector() {
    this.addInput("Messages", "messages");
    this.addOutput("Path A", "messages");
    this.addOutput("Path B", "messages");
    this.title = "⚖️ MESSAGE SELECTOR";
}
LiteGraph.registerNodeType("hub/selector", NodeSelector);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        history = await engine._resolve_input(node, 0) or []
        user_text = (history[-1].get("content", "") if history else "").lower()
        
        cond_a = node["properties"].get("condition_a", "").lower()
        cond_b = node["properties"].get("condition_b", "").lower()
        
        # Determine if we should yield data for this specific output slot
        if output_slot_idx == 0 and cond_a and cond_a in user_text:
            return history
        if output_slot_idx == 1 and cond_b and cond_b in user_text:
            return history
        if output_slot_idx == 2: # Default path
            return history
            
        return None

class MOENode(BaseNode):
    node_type = "hub/moe"
    node_title = "Advanced MoE"
    node_category = "Logic & Routing"
    node_icon = "✨"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeMoE() {
    this.addInput("User Context", "messages");
    this.addInput("Expert 1", "expert,string");
    this.addOutput("Synthesized", "string");
    this.title = "✨ MIXTURE OF EXPERTS";
}
LiteGraph.registerNodeType("hub/moe", NodeMoE);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        props = node.get("properties", {})
        history = await engine._resolve_input(node, 0) or []
        
        expert_tasks, expert_names = [], []
        
        # Collect experts from dynamic inputs
        for i in range(1, len(node.get("inputs", []))):
            inp = node["inputs"][i]
            if inp.get("name") == "Settings" or not inp.get("link"): continue
            
            link = engine.links.get(inp["link"])
            src_name = engine.nodes[link[1]].get("properties", {}).get("model", f"Expert {i}") if link and link[1] in engine.nodes else f"Expert {i}"
            expert_names.append(src_name)
            expert_tasks.append(engine.execute_cognitive_path(inp["link"], history))
                
        if not expert_tasks: return "No experts connected."
        
        if props.get("send_status_update"):
            event_manager.emit(ProxyEvent("active", engine.request_id, "MoE Block", "Gateway", engine.sender, error_message=f"Engaging: {', '.join(expert_names)}..."))

        # Run experts in parallel
        responses = await asyncio.gather(*expert_tasks, return_exceptions=True)
        panel_data = ""
        for i, resp in enumerate(responses):
            val = str(resp) if not isinstance(resp, Exception) else f"Error: {str(resp)}"
            panel_data += f"### EXPERT {i+1} FEEDBACK:\\n{val}\\n\\n"

        orchestrator_model = props.get("orchestrator", "auto")
        final_messages = list(history) if isinstance(history, list) else [{"role": "user", "content": str(history)}]
        
        # Synthesis Prompt
        final_messages.append({"role": "user", "content": f"### EXPERT PANEL OUTPUTS\\n{panel_data}\\n\\n### SYNTHESIS MANDATE\\n{props.get('system_prompt', '')}"})
        
        servers = await server_crud.get_servers_with_model(engine.db, orchestrator_model)
        if not servers: return f"[Error] Orchestrator '{orchestrator_model}' offline."
        
        payload = {"model": orchestrator_model, "messages": final_messages, "stream": False}
        resp_obj, _ = await engine.reverse_proxy_fn(engine.request, "chat", servers, json.dumps(payload).encode(), is_subrequest=True, sender="graph-moe")
        
        if hasattr(resp_obj, 'body'):
            return json.loads(resp_obj.body.decode()).get("message", {}).get("content", "Error: Empty")
        return "Synthesis Error: No response."