function NodeInput() {
    this.title = "ENTRY: REQUEST MESSAGES";
    this.addOutput("Messages", "messages");
    this.addOutput("Settings", "object");
    this.addOutput("Input", "string");
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.color = "#1e3a8a";
    this.bgcolor = "#172554";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeInput.title = "ENTRY: REQUEST MESSAGES";
NodeInput.prototype.onConfigure = function() {
    if (!this.title || this.title === "NodeInput") this.title = "ENTRY: REQUEST MESSAGES";
};
LiteGraph.registerNodeType("hub/input", NodeInput);

function NodeOutput() {
    this.title = "EXIT: GATEWAY RESPONSE";
    this.addInput("Source", "messages,string");
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.color = "#064e3b";
    this.bgcolor = "#022c22";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeOutput.title = "EXIT: GATEWAY RESPONSE";
NodeOutput.prototype.onConfigure = function() {
    if (!this.title || this.title === "NodeOutput") this.title = "EXIT: GATEWAY RESPONSE";
};
LiteGraph.registerNodeType("hub/output", NodeOutput);