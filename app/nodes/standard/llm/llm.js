function NodeLLMChat() {
    this.addInput("Messages", "messages");
    this.addInput("Settings", "object");
    this.addInput("Model Override", "string");
    this.addOutput("Content", "string");
    this.properties = { model: "auto" };
    
    this.mWidget = this.addWidget("combo", "Model", this.properties.model, (v) => { 
        this.properties.model = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: window.available_models || ["auto"] });

    this.addWidget("button", "+ Add Tool", null, () => {
        this.addInput("Tool " + (this.inputs.length - 2), "tool,array");
        this.size = this.computeSize();
        if(this.setDirtyCanvas) this.setDirtyCanvas(true, true);
        if(window.pushHistoryState) window.pushHistoryState();
    });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "💬 LLM CHAT";
    this.color = "#312e81";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}

NodeLLMChat.title = "💬 LLM CHAT";
NodeLLMChat.prototype.onConfigure = function(config) {
    if (this.mWidget) this.mWidget.value = this.properties.model;
    if (!this.title || this.title === "NodeLLMChat") this.title = "💬 LLM CHAT";
};
LiteGraph.registerNodeType("hub/llm_chat", NodeLLMChat);