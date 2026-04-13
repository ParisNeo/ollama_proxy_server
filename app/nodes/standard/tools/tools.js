function NodeToolSelector() {
    this.addOutput("Tool Schema", "tool");
    this.properties = { library: "", function: "" };
    
    this.lWidget = this.addWidget("combo", "Library", this.properties.library, (v) => { 
        this.properties.library = v; 
        this.refreshFunctions();
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values:[] });
    
    this.fWidget = this.addWidget("combo", "Function", this.properties.function, (v) => { 
        this.properties.function = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values:[] });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "🛠️ TOOL SELECTOR";
    this.color = "#9f1239";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeToolSelector.title = "🛠️ TOOL SELECTOR";
NodeToolSelector.prototype.onConfigure = function() {
    if (this.lWidget) this.lWidget.value = this.properties.library;
    if (this.fWidget) this.fWidget.value = this.properties.function;
    if (!this.title || this.title === "NodeToolSelector") this.title = "🛠️ TOOL SELECTOR";
};
NodeToolSelector.prototype.onAdded = async function() {
    try {
        const resp = await fetch("/api/v1/api/tools");
        this.allToolsData = await resp.json();
        this.lWidget.options.values = this.allToolsData.map(t => t.filename);
        if (this.properties.library) this.refreshFunctions(false);
    } catch(e) { console.error("Tool fetch failed", e); }
};
NodeToolSelector.prototype.refreshFunctions = function(resetSelection = true) {
    const lib = this.allToolsData?.find(t => t.filename === this.properties.library);
    if (lib) {
        const matches = [...lib.raw.matchAll(/def (tool_[\w_]+)/g)];
        const fns = ["[ALL FUNCTIONS]"].concat(matches.map(m => m[1]));
        this.fWidget.options.values = fns;
        if (resetSelection && fns.length > 0) {
            this.properties.function = fns[0];
            this.fWidget.value = fns[0];
        }
    } else {
        this.fWidget.options.values =[];
    }
};
LiteGraph.registerNodeType("hub/tool_selector", NodeToolSelector);