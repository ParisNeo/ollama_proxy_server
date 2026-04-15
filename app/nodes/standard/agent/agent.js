function NodeAgent() {
    this.addInput("In Messages", "messages");
    this.addInput("Settings", "object");
    this.addOutput("Final Answer", "string");
    this.addOutput("Out Messages", "messages");
    this.properties = { model: "auto", max_turns: 10, memory_system: "none" };
    
    this.mWidget = this.addWidget("combo", "Model", this.properties.model, (v) => { 
        this.properties.model = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: window.available_models || ["auto"] });
    
    this.tWidget = this.addWidget("number", "Max Turns", this.properties.max_turns, (v) => { 
        this.properties.max_turns = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { min: 1, max: 30 });

    this.msWidget = this.addWidget("combo", "Memory Core", this.properties.memory_system, (v) => {
        this.properties.memory_system = v;
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: ["none"].concat(window.memory_systems_list ||["default"]) });
    
    this.addWidget("button", "+ Add Tool Slot", null, () => {
        this.addInput("Tool " + (this.inputs.length - 1), "tool,mcp");
        this.size = this.computeSize();
        if(this.setDirtyCanvas) this.setDirtyCanvas(true, true);
        if(window.pushHistoryState) window.pushHistoryState();
    });
    
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "🧠 AUTONOMOUS AGENT";
    this.color = "#be123c";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeAgent.title = "🧠 AUTONOMOUS AGENT";
NodeAgent.prototype.onConfigure = function() {
    if (this.mWidget) this.mWidget.value = this.properties.model;
    if (this.tWidget) this.tWidget.value = this.properties.max_turns;
    if (this.msWidget) this.msWidget.value = this.properties.memory_system;
    if (!this.title || this.title === "NodeAgent") this.title = "🧠 AUTONOMOUS AGENT";
};
NodeAgent.prototype.onAdded = function() {
    if (this.msWidget && window.memory_systems_list) {
        this.msWidget.options.values = ["none"].concat(window.memory_systems_list);
    }
};
LiteGraph.registerNodeType("hub/agent", NodeAgent);