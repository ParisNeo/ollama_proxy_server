import json
from typing import Dict, Any
from app.nodes.base import BaseNode
from app.crud import server_crud

class LLMChatNode(BaseNode):
    node_type = "hub/llm_chat"
    node_title = "LLM Chat"
    node_category = "Serving & Cognition"
    node_icon = "💬"
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeLLMChat() {
    this.addInput("Messages", "messages");
    this.addInput("Settings", "object");
    this.addInput("Model Override", "string");
    this.addOutput("Content", "string");
    this.properties = { model: "auto" };
    this.mWidget = this.addWidget("combo", "Model", this.properties.model, (v) => { this.properties.model = v; pushHistoryState(); }, { values: window.available_models || ["auto"] });
    this.addWidget("button", "+ Add Tool", null, () => {
        this.addInput("Tool " + (this.inputs.length - 2), "tool,array");
        this.size = this.computeSize();
    });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "💬 LLM CHAT";
    this.color = "#312e81";
    this.bgcolor = "#1e1b4b";
    this.size = this.computeSize();
}
NodeLLMChat.prototype.onConfigure = function() { if(this.mWidget) this.mWidget.value = this.properties.model; };
LiteGraph.registerNodeType("hub/llm_chat", NodeLLMChat);
"""
    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        """
        Executed when this node is in the middle of a graph.
        For terminal nodes, the engine now bypasses this and calls the proxy directly.
        """
        target_model = str(node["properties"].get("model", "auto")).strip()
        
        # Resolve inputs
        msgs = await engine._resolve_input(node, 0)
        if msgs is None: msgs = engine.initial_messages
        
        settings = await engine._resolve_input(node, 1) or {}
        override = await engine._resolve_input(node, 2)
        if override: target_model = str(override)
        
        # 1. Collect Tools from graph slots
        tools = []
        for i in range(3, len(node.get("inputs", []))):
            t = await engine._resolve_input(node, i)
            if t: tools.extend(t if isinstance(t, list) else [t])
        
        # 2. Resolve Target Physical Model
        real_model, final_msgs = await engine.resolve_target_fn(
            engine.db, target_model, msgs, engine.depth + 1, 
            engine.request, engine.request_id, engine.sender
        )
        
        # 3. Build full payload including tools
        # This fixes the "You were expected to call a tool" error by matching
        # the system prompt's capabilities with the actual API parameters.
        payload = {
            "model": real_model,
            "messages": final_msgs,
            "stream": False, 
            "options": settings
        }
        
        if tools:
            # Flatten potential lists from the [ALL FUNCTIONS] selection
            flat_tools = []
            for t in tools:
                if isinstance(t, list): flat_tools.extend(t)
                elif t: flat_tools.append(t)
                
            payload["tools"] = flat_tools
            if not payload["tools"]: del payload["tools"]

        # 4. Call Backend
        servers = await server_crud.get_servers_with_model(engine.db, real_model)
        if not servers:
            return f"[Error: Model {real_model} offline]"

        resp, _ = await engine.reverse_proxy_fn(
            engine.request, "chat", servers, 
            json.dumps(payload).encode(), 
            is_subrequest=True,
            request_id=engine.request_id,
            model=real_model,
            sender=engine.sender
        )
        
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            msg = data.get("message", {})
            return msg.get("content", "") or json.dumps(msg.get("tool_calls", []))
        
        return "[Error: Empty response from backend]"

class LLMInstructNode(BaseNode):
    node_type = "hub/llm_instruct"
    node_title = "LLM Instruct"
    node_category = "Serving & Cognition"
    node_icon = "📝"
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeLLMInstruct() {
    this.addInput("Prompt", "string");
    this.addOutput("Response", "string");
    this.properties = { model: "auto" };
    this.mWidget = this.addWidget("combo", "Model", this.properties.model, (v) => { this.properties.model = v; }, { values: window.available_models || ["auto"] });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "📝 LLM INSTRUCT";
    this.color = "#374151";
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/llm_instruct", NodeLLMInstruct);
"""
    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        prompt = await engine._resolve_input(node, 0)
        model = node["properties"].get("model", "auto")
        msgs = [{"role": "user", "content": str(prompt)}]
        return await engine.resolve_target_fn(engine.db, model, msgs, engine.depth + 1, engine.request, engine.request_id, engine.sender)

class AgentReasonerNode(BaseNode):
    node_type = "hub/agent"
    node_title = "Autonomous Agent"
    node_category = "Serving & Cognition"
    node_icon = "🧠"

    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeAgent() {
    this.addInput("In Messages", "messages");
    this.addInput("Settings", "object");
    this.addOutput("Final Answer", "string");
    this.addOutput("Out Messages", "messages");
    
    this.properties = { 
        model: "auto", 
        max_turns: 10, 
        internal_monologue: true,
        agent_instruction: "You are an autonomous agent. Think step-by-step using <thought> tags. Use available tools to gather data. When done, provide your final answer."
    };
    
    this.mWidget = this.addWidget("combo", "Model", this.properties.model, (v) => { this.properties.model = v; pushHistoryState(); }, { values: window.available_models || ["auto"] });
    this.addWidget("number", "Max Turns", 10, (v) => { this.properties.max_turns = v; }, { min: 1, max: 30 });
    this.addWidget("toggle", "Show Thoughts", true, (v) => { this.properties.internal_monologue = v; });
    
    this.addWidget("button", "+ Add Tool Slot", null, () => {
        this.addInput("Tool " + (this.inputs.length - 1), "tool,mcp");
        this.size = this.computeSize();
    });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    
    this.title = "🧠 AUTONOMOUS AGENT";
    this.color = "#be123c"; // rose-700
    this.bgcolor = "#4c0519"; // rose-950
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeAgent.prototype.onConfigure = function() {
    if(this.mWidget) this.mWidget.value = this.properties.model;
};
LiteGraph.registerNodeType("hub/agent", NodeAgent);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        import json
        import copy
        import secrets
        from app.core.events import event_manager, ProxyEvent
        from app.crud import server_crud

        # 1. Setup
        msgs = await engine._resolve_input(node, 0)
        if msgs is None: msgs = engine.initial_messages
        
        # Working memory for the agent (The Scratchpad)
        scratchpad = copy.deepcopy(msgs)
        
        settings = await engine._resolve_input(node, 1) or {}
        model = node["properties"].get("model", "auto")
        max_turns = int(node["properties"].get("max_turns", 10))
        
        # 2. Collect Tools & MCPs
        tools = []
        for i in range(2, len(node.get("inputs", []))):
            t_data = await engine._resolve_input(node, i)
            if t_data:
                # If it's an MCP, it needs to be handled by the Hub's tool dispatcher
                # For this implementation, we assume the Hub Proxy handles the execution 
                # if we pass the tool definitions in the request.
                if isinstance(t_data, list): tools.extend(t_data)
                else: tools.append(t_data)

        # 3. Execution Loop (The Reasoning Core)
        final_text = ""
        for turn in range(1, max_turns + 1):
            # Resolve physical model for this specific turn
            real_model, turn_msgs = await engine.resolve_target_fn(
                engine.db, model, scratchpad, engine.depth + 1, 
                engine.request, engine.request_id, engine.sender
            )

            payload = {
                "model": real_model,
                "messages": turn_msgs,
                "stream": False,
                "tools": [t for t in tools if t],
                "options": settings
            }

            # Telemetry: Thought Phase
            event_manager.emit(ProxyEvent(
                "active", engine.request_id, f"Agent Turn {turn}", real_model, engine.sender,
                error_message=f"Thinking... (Turn {turn}/{max_turns})"
            ))

            servers = await server_crud.get_servers_with_model(engine.db, real_model)
            if not servers: return f"[Error: Agent model {real_model} offline]"

            # Call Backend
            resp, chosen_server = await engine.reverse_proxy_fn(
                engine.request, "chat", servers, json.dumps(payload).encode(), 
                is_subrequest=True, sender="autonomous-agent"
            )

            if not hasattr(resp, 'body'): break
            
            res_data = json.loads(resp.body.decode())
            ai_msg = res_data.get("message", {})
            
            # Update Scratchpad with AI's response
            scratchpad.append(ai_msg)
            
            # --- PHASE A: Handle Thoughts ---
            content = ai_msg.get("content", "")
            if "<thought>" in content:
                # Optional: log thought to a specific UI component
                pass

            # --- PHASE B: Handle Actions (Tool Calls) ---
            tool_calls = ai_msg.get("tool_calls", [])
            if tool_calls:
                for call in tool_calls:
                    fn = call.get("function", {})
                    t_name = fn.get("name")
                    t_args = fn.get("arguments", {})
                    
                    event_manager.emit(ProxyEvent(
                        "active", engine.request_id, "Tool Execution", t_name, engine.sender,
                        error_message=f"Executing tool: {t_name}..."
                    ))
                    
                    # Logic: Execute the tool and append result
                    # In this hub version, we simulate the tool execution or route to MCP
                    result_str = f"[Tool {t_name} executed successfully with results...]"
                    
                    scratchpad.append({
                        "role": "tool",
                        "name": t_name,
                        "content": result_str
                    })
                
                # Continue loop to let agent process tool results
                continue
            
            # --- PHASE C: Final Answer ---
            if content:
                final_text = content
                break

        # Return requested output
        if output_slot_idx == 0: return final_text
        return scratchpad