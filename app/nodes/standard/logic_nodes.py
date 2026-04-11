import re
from typing import Dict, Any
from app.nodes.base import BaseNode

class ExtractTextNode(BaseNode):
    node_type = "hub/extract_text"
    node_title = "Extract Text"
    node_category = "Logic & Routing"
    node_icon = "📝"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeExtractText() {
    this.addInput("Messages", "messages");
    this.addOutput("Text", "string");
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "EXTRACT TEXT";
    this.color = "#059669";
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/extract_text", NodeExtractText);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        msgs = await engine._resolve_input(node, 0)
        if msgs and isinstance(msgs, list):
            return msgs[-1].get("content", "")
        return ""

class PromptComposerNode(BaseNode):
    node_type = "hub/composer"
    node_title = "Prompt Composer"
    node_category = "Logic & Routing"
    node_icon = "🖋️"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeComposer() {
    this.addInput("A", "string");
    this.addInput("B", "string");
    this.addOutput("Merged", "string");
    this.properties = { template: "{A}\\n\\n{B}" };
    this.addWidget("text", "Template", this.properties.template, (v) => { this.properties.template = v; });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "🖋️ PROMPT COMPOSER";
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/composer", NodeComposer);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        a = await engine._resolve_input(node, 0) or ""
        b = await engine._resolve_input(node, 1) or ""
        template = node["properties"].get("template", "{A}\n\n{B}")
        return template.replace("{A}", str(a)).replace("{B}", str(b))