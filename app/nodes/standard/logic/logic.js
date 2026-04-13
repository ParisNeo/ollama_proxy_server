function NodeExtractText() {
    this.addInput("Messages", "messages");
    this.addOutput("Text", "string");
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "📝 EXTRACT TEXT";
    this.color = "#059669";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeExtractText.title = "📝 EXTRACT TEXT";
NodeExtractText.prototype.onConfigure = function() {
    if (!this.title || this.title === "NodeExtractText") this.title = "📝 EXTRACT TEXT";
};
LiteGraph.registerNodeType("hub/extract_text", NodeExtractText);