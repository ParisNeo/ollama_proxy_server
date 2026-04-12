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
    this.properties = { 
        name: "Google Knowledge", 
        url: "https://developerknowledge.googleapis.com/mcp", 
        type: "sse",
        auth_type: "api_key",
        auth_token: "",
        headers_json: "{}"
    };
    
    this.addWidget("text", "Name", this.properties.name, (v) => { this.properties.name = v; this.title = "🔌 MCP: " + v.toUpperCase(); });
    this.addWidget("text", "Endpoint URL", this.properties.url, (v) => { this.properties.url = v; });
    
    this.addWidget("combo", "Auth Type", this.properties.auth_type, (v) => { this.properties.auth_type = v; }, { values: ["none", "api_key", "bearer"] });
    this.addWidget("text", "Credentials/Key", this.properties.auth_token, (v) => { this.properties.auth_token = v; });
    
    this.addWidget("text", "Extra Headers (JSON)", this.properties.headers_json, (v) => { this.properties.headers_json = v; });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    
    this.title = "🔌 MCP: " + this.properties.name.toUpperCase();
    this.color = "#7c3aed";
    this.bgcolor = "#4c1d95";
    this.serialize_widgets = true;
    this.size = [350, 180];
}
LiteGraph.registerNodeType("hub/mcp", NodeMCP);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        props = node.get("properties", {})
        
        headers = {}
        try:
            if props.get("headers_json"):
                headers = json.loads(props["headers_json"])
        except: pass

        token = props.get("auth_token", "")
        if props.get("auth_type") == "api_key":
            headers["x-goog-api-key"] = token # Standard Google MCP header
            headers["Authorization"] = f"Bearer {token}" # Fallback
        elif props.get("auth_type") == "bearer":
            headers["Authorization"] = f"Bearer {token}"

        return {
            "type": "mcp", 
            "name": props.get("name"), 
            "config": {
                "url": props.get("url"), 
                "type": "sse",
                "headers": headers
            }
        }