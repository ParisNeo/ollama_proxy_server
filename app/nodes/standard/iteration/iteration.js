function NodeForLoop() {
    this.addInput("List/Range", "array,string");
    this.addOutput("Item", 0);
    this.addOutput("Combined", "array");
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "🔄 FOR LOOP";
    this.color = "#0891b2";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeForLoop.title = "🔄 FOR LOOP";
NodeForLoop.prototype.onConfigure = function() {
    if (!this.title || this.title === "NodeForLoop") this.title = "🔄 FOR LOOP";
};
LiteGraph.registerNodeType("hub/for_loop", NodeForLoop);

function NodeWhileLoop() {
    this.addInput("Initial Data", 0);
    this.addInput("Condition", "boolean,string");
    this.addOutput("Loop Body", 0);
    this.addOutput("Result", 0);
    this.properties = { max_iterations: 5 };
    
    this.iWidget = this.addWidget("number", "Max Iterations", this.properties.max_iterations, (v) => { 
        this.properties.max_iterations = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { min: 1, max: 20 });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "♾️ WHILE LOOP";
    this.color = "#0891b2";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeWhileLoop.title = "♾️ WHILE LOOP";
NodeWhileLoop.prototype.onConfigure = function() {
    if (this.iWidget) this.iWidget.value = this.properties.max_iterations;
    if (!this.title || this.title === "NodeWhileLoop") this.title = "♾️ WHILE LOOP";
};
LiteGraph.registerNodeType("hub/while_loop", NodeWhileLoop);