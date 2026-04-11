import json
from typing import Dict, Any
from app.nodes.base import BaseNode

class ToolDefinitionNode(BaseNode):
    node_type = "hub/tool"
    node_title = "Tool Definition"
    node_category = "Knowledge & RAG"
    node_icon = "🔧"

    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeTool() {
    this.addOutput("Tool", "tool");
    this.properties = { 
        name: "get_weather", 
        description: "Get current weather", 
        parameters: { type: "object", properties: { location: { type: "string" } } },
        raw: "" 
    };
    this.addWidget("button", "Configure Tool Schema", null, () => {
        window.openMarkdownModal(this, 'tool');
    });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "🔧 TOOL: " + this.properties.name.toUpperCase();
    this.color = "#b91c1c";
    this.bgcolor = "#7f1d1d";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/tool", NodeTool);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        props = node.get("properties", {})
        # If the user used the Markdown modal, use the parsed metadata
        if props.get("metadata"):
            meta = props["metadata"]
            return {
                "type": "function",
                "function": {
                    "name": meta.get("name", props.get("name")),
                    "description": meta.get("description", props.get("description")),
                    "parameters": meta.get("parameters", props.get("parameters"))
                }
            }
        
        # Fallback to direct properties
        return {
            "type": "function",
            "function": {
                "name": props.get("name"),
                "description": props.get("description"),
                "parameters": props.get("parameters")
            }
        }

class MCPConfigNode(BaseNode):
    node_type = "hub/mcp"
    node_title = "MCP Server"
    node_category = "Knowledge & RAG"
    node_icon = "🔌"

    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeMCP() {
    this.addOutput("MCP", "mcp");
    this.properties = { name: "Local RAG", url: "http://localhost:3010", type: "sse" };
    this.addWidget("text", "Name", this.properties.name, (v) => { this.properties.name = v; this.title = "🔌 MCP: " + v.toUpperCase(); });
    this.addWidget("text", "URL/Command", this.properties.url, (v) => { this.properties.url = v; });
    this.addWidget("combo", "Type", this.properties.type, (v) => { this.properties.type = v; }, { values: ["sse", "stdio"] });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "🔌 MCP: " + this.properties.name.toUpperCase();
    this.color = "#7c3aed";
    this.bgcolor = "#4c1d95";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/mcp", NodeMCP);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        props = node.get("properties", {})
        return {
            "type": "mcp",
            "name": props.get("name"),
            "config": {
                "url": props.get("url"),
                "type": props.get("type")
            }
        }