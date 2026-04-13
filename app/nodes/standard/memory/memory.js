function NodeMemoryLoader() {
    this.addInput("Key Override", "string");
    this.addOutput("Memory Content", "string");
    this.properties = { key: "" };
    this.kWidget = this.addWidget("text", "Default Key", this.properties.key, (v) => { 
        this.properties.key = v;
        if(window.pushHistoryState) window.pushHistoryState();
    });
    this.title = "🧠 MEMORY LOADER";
    this.color = "#a855f7"; // purple-500
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeMemoryLoader.prototype.onConfigure = function() {
    if (this.kWidget) this.kWidget.value = this.properties.key;
};
LiteGraph.registerNodeType("hub/memory_loader", NodeMemoryLoader);