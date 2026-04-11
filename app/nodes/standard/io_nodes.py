from typing import Dict, Any
from app.nodes.base import BaseNode

class InputNode(BaseNode):
    node_type = "hub/input"
    node_title = "Request Input"
    node_category = "IO & Terminal"
    node_icon = "📥"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeInput() {
    this.title = "ENTRY: REQUEST MESSAGES";
    this.addOutput("Messages", "messages");
    this.addOutput("Settings", "object");
    this.addOutput("Input", "string");
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.color = "#1e3a8a";
    this.bgcolor = "#172554";
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/input", NodeInput);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        # Slot 0: Messages, Slot 1: Settings, Slot 2: Raw Input String
        if output_slot_idx == 0: return engine.initial_messages
        if output_slot_idx == 1: return {}
        if output_slot_idx == 2: 
            return engine.initial_messages[-1].get("content", "") if engine.initial_messages else ""
        return None

class OutputNode(BaseNode):
    node_type = "hub/output"
    node_title = "Global Output"
    node_category = "IO & Terminal"
    node_icon = "📤"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeOutput() {
    this.title = "EXIT: GATEWAY RESPONSE";
    this.addInput("Source", "messages,string");
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.color = "#064e3b";
    this.bgcolor = "#022c22";
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/output", NodeOutput);
"""
    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        # Output nodes don't return values to other nodes; they provide the final result.
        return await engine._resolve_input(node, 0)