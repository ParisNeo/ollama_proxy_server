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
    node_title = "Mixture of Experts"
    node_category = "Serving & Cognition"
    node_icon = "✨"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeMoE() {
    this.addInput("User Context", "messages");
    this.addInput("Expert 1", "expert,string");
    this.addOutput("Final Output", "string");
    
    this.properties = { 
        orchestrator: "auto", 
        show_intermediate: true,
        send_status: true,
        system_prompt: "You are the Lead Synthesis Architect. Review the following panel of expert responses and provide a unified, high-quality final answer that incorporates the best insights from all experts."
    };
    
    this.mWidget = this.addWidget("combo", "Orchestrator", this.properties.orchestrator, (v) => { this.properties.orchestrator = v; }, { values: ["auto"].concat(window.available_models) });
    this.addWidget("toggle", "Intermediate Content", this.properties.show_intermediate, (v) => { this.properties.show_intermediate = v; });
    this.addWidget("toggle", "Processing Status", this.properties.send_status, (v) => { this.properties.send_status = v; });
    
    this.addWidget("button", "+ Add Expert Slot", null, () => {
        this.addInput("Expert " + (this.inputs.length), "expert,string");
        this.size = this.computeSize();
    });

    this.title = "✨ MIXTURE OF EXPERTS";
    this.color = "#7c3aed";
    this.bgcolor = "#4c1d95";
    this.size = [320, 160];
    this.serialize_widgets = true;
}
NodeMoE.prototype.onConfigure = function() {
    if(this.mWidget) this.mWidget.value = this.properties.orchestrator;
};
LiteGraph.registerNodeType("hub/moe", NodeMoE);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        import asyncio
        import json
        from app.core.events import event_manager, ProxyEvent
        from app.crud import server_crud

        props = node.get("properties", {})
        history = await engine._resolve_input(node, 0) or engine.initial_messages
        
        expert_tasks, expert_names = [], []
        status_lines = []

        # 1. Identify and setup Expert calls
        for i in range(1, len(node.get("inputs", []))):
            inp = node["inputs"][i]
            if not inp.get("link"): continue
            
            link = engine.links.get(inp["link"])
            # Try to resolve a friendly name for the expert
            src_node = engine.nodes.get(link[1])
            src_name = src_node.get("properties", {}).get("model", f"Expert {i}") if src_node else f"Expert {i}"
            
            expert_names.append(src_name)
            status_lines.append(f"- {src_name} is thinking...")
            expert_tasks.append(engine.execute_cognitive_path(inp["link"], history))
                
        if not expert_tasks: return "Error: No experts connected to MoE node."

        # 2. Parallel Execution & Status Emitting
        processing_block = ""
        if props.get("send_status"):
            processing_block = "<processing>\n" + "\n".join(status_lines) + "\n</processing>\n\n"
            # Emit live telemetry
            event_manager.emit(ProxyEvent(
                "active", engine.request_id, "Parallel Brainstorm", "MoE Engine", 
                engine.sender, error_message=f"Polling {len(expert_tasks)} experts..."
            ))

        responses = await asyncio.gather(*expert_tasks, return_exceptions=True)
        
        panel_data = ""
        for i, resp in enumerate(responses):
            name = expert_names[i]
            val = str(resp) if not isinstance(resp, Exception) else f"Error: {str(resp)}"
            panel_data += f"### EXPERT: {name}\n{val}\n\n"

        # 3. Final Synthesis
        status_lines.append("- Finalizing answer...")
        if props.get("send_status"):
             processing_block = "<processing>\n" + "\n".join(status_lines) + "\n</processing>\n\n"

        orchestrator_model = props.get("orchestrator", "auto")
        final_messages = list(history) if isinstance(history, list) else [{"role": "user", "content": str(history)}]
        
        # Inject context for synthesis
        final_messages.append({"role": "user", "content": f"### EXPERT PANEL FEEDBACK:\n{panel_data}\n\n### MANDATE:\n{props.get('system_prompt')}"})
        
        servers = await server_crud.get_servers_with_model(engine.db, orchestrator_model)
        if not servers: return f"{processing_block}[Error: Orchestrator model '{orchestrator_model}' offline]"

        resp_obj, _ = await engine.reverse_proxy_fn(
            engine.request, "chat", servers, 
            json.dumps({"model": orchestrator_model, "messages": final_messages, "stream": False}).encode(), 
            is_subrequest=True, sender="moe-orchestrator"
        )
        
        final_answer = ""
        if hasattr(resp_obj, 'body'):
            data = json.loads(resp_obj.body.decode())
            final_answer = data.get("message", {}).get("content", "Error: Empty response.")
        
        # 4. Construct Final String
        output = processing_block
        if props.get("show_intermediate"):
            output += "## Intermediate Expert Insights\n" + panel_data + "\n---\n"
        
        output += "## Final Answer\n" + final_answer
        return output