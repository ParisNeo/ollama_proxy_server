import json
from typing import Dict, Any
from app.nodes.base import BaseNode

class ToolSelectorNode(BaseNode):
    node_type = "hub/tool_selector"
    node_title = "Tool Selector"
    node_category = "Selectors"
    node_icon = "🛠️"

    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeToolSelector() {
    this.addOutput("Tool Schema", "tool");
    this.properties = { library: "", function: "" };
    
    this.lWidget = this.addWidget("combo", "Library", this.properties.library, (v) => { 
        this.properties.library = v; 
        this.refreshFunctions();
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: [] });

    this.fWidget = this.addWidget("combo", "Function", this.properties.function, (v) => { 
        this.properties.function = v; 
        this.title = "🛠️ " + v.toUpperCase();
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: [] });

    this.addWidget("button", "ℹ️ Help", null, () => { showNodeHelp(this.type); });
    
    this.title = "🛠️ TOOL SELECTOR";
    this.color = "#9f1239";
    this.bgcolor = "#4c0519";
    this.serialize_widgets = true;
    this.size = [280, 110];
}

NodeToolSelector.prototype.onAdded = async function() {
    await this.syncData();
};

NodeToolSelector.prototype.syncData = async function() {
    try {
        const resp = await fetch("/api/v1/api/tools");
        this.allToolsData = await resp.json();
        this.lWidget.options.values = this.allToolsData.map(t => t.filename);
        if (this.properties.library) this.refreshFunctions(false);
    } catch(e) { console.error("Tool sync error", e); }
};

NodeToolSelector.prototype.refreshFunctions = function(resetSelection = true) {
    const lib = this.allToolsData?.find(t => t.filename === this.properties.library);
    if (lib) {
        const matches = [...lib.raw.matchAll(/def (tool_[\\w_]+)/g)];
        // Add "All Functions" option at the top
        const fns = ["[ALL FUNCTIONS]"].concat(matches.map(m => m[1]));
        this.fWidget.options.values = fns;
        if (resetSelection && fns.length > 0) {
            this.properties.function = fns[0];
            this.fWidget.value = fns[0];
            this.title = "🛠️ " + fns[0].toUpperCase();
        }
    } else {
        this.fWidget.options.values = [];
    }
};

NodeToolSelector.prototype.onConfigure = function() {
    this.syncData();
};

LiteGraph.registerNodeType("hub/tool_selector", NodeToolSelector);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        from app.core.tools_manager import ToolsManager
        lib_name = node["properties"].get("library")
        fn_name = node["properties"].get("function")
        if not lib_name or not fn_name: return None
        all_tools = ToolsManager.get_all_tools()
        lib = next((t for t in all_tools if t["filename"] == lib_name), None)
        if not lib: return None
        
        schemas = ToolsManager.get_tool_definitions(lib["raw"])
        
        # BULK SELECTION LOGIC
        if fn_name == "[ALL FUNCTIONS]":
            for s in schemas: s["library"] = lib_name
            return schemas
            
        for s in schemas:
            if s["function"]["name"] == fn_name:
                s["library"] = lib_name
                return s
        return None

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
        return {"type": "mcp", "name": props.get("name"), "config": {"url": props.get("url"), "type": props.get("type")}}