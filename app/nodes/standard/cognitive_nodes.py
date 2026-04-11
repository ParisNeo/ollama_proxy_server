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
        target_model = str(node["properties"].get("model", "auto")).strip()
        msgs = await engine._resolve_input(node, 0) or engine.initial_messages
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
            # Filter out None and deduplicate
            payload["tools"] = [t for t in tools if t]
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
    node_title = "Agentic Reasoner"
    node_category = "Serving & Cognition"
    node_icon = "🤖"
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeAgent() {
    this.addInput("Messages", "messages");
    this.addOutput("Content", "string");
    this.properties = { model: "auto", max_loops: 5 };
    this.addWidget("combo", "Model", this.properties.model, (v) => { this.properties.model = v; }, { values: window.available_models || ["auto"] });
    this.addWidget("number", "Max Loops", this.properties.max_loops, (v) => { this.properties.max_loops = v; }, { min: 1, max: 20 });
    this.title = "🤖 AGENT REASONER";
    this.color = "#f43f5e";
}
LiteGraph.registerNodeType("hub/agent", NodeAgent);
"""
    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        # Complex multi-step reasoning logic handled by engine._evaluate_node fallback for now
        return await engine._evaluate_node(node, output_slot_idx)