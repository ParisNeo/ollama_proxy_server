function NodeVision() {
    this.addInput("In Messages", "messages");
    this.addOutput("Out Messages", "messages");
    this.properties = { vlm: "auto" };
    
    this.vWidget = this.addWidget("combo", "VLM Model", this.properties.vlm, (v) => { 
        this.properties.vlm = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: window.available_models || ["auto"] });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "👁️ VISION HYDRATOR";
    this.color = "#0369a1";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeVision.title = "👁️ VISION HYDRATOR";
NodeVision.prototype.onConfigure = function() {
    if (this.vWidget) this.vWidget.value = this.properties.vlm;
    if (!this.title || this.title === "NodeVision") this.title = "👁️ VISION HYDRATOR";
};
LiteGraph.registerNodeType("hub/vision", NodeVision);