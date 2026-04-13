function NodeIfElse() {
    this.addInput("Value", "string,messages");
    this.addOutput("True Path", "string,messages");
    this.addOutput("False Path", "string,messages");
    this.properties = { condition: "", mode: "contains" };
    
    this.mWidget = this.addWidget("combo", "Mode", this.properties.mode, (v) => { 
        this.properties.mode = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: ["contains", "equals", "regex", "exists"] });
    
    this.cWidget = this.addWidget("text", "Condition", this.properties.condition, (v) => { 
        this.properties.condition = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "⚖️ IF / ELSE";
    this.color = "#4b5563";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeIfElse.title = "⚖️ IF / ELSE";
NodeIfElse.prototype.onConfigure = function() {
    if (this.mWidget) this.mWidget.value = this.properties.mode;
    if (this.cWidget) this.cWidget.value = this.properties.condition;
    if (!this.title || this.title === "NodeIfElse") this.title = "⚖️ IF / ELSE";
};
LiteGraph.registerNodeType("hub/if_else", NodeIfElse);

function NodeSwitchCase() {
    this.addInput("Input", 0);
    this.addOutput("Default", 0);
    this.properties = { cases:[] };
    
    this.addWidget("button", "+ Add Case", null, () => {
        const caseVal = prompt("Enter case value:");
        if (caseVal !== null) {
            this.properties.cases.push(caseVal);
            this.refreshSlots();
        }
    });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "⑂ SWITCH / CASE";
    this.color = "#4b5563";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeSwitchCase.title = "⑂ SWITCH / CASE";
NodeSwitchCase.prototype.refreshSlots = function() {
    this.outputs =[];
    this.properties.cases.forEach((c) => {
        this.addOutput("Case: " + c, 0);
    });
    this.addOutput("Default", 0);
    this.size = this.computeSize();
    if(window.pushHistoryState) window.pushHistoryState();
};
NodeSwitchCase.prototype.onConfigure = function() {
    this.refreshSlots();
    if (!this.title || this.title === "NodeSwitchCase") this.title = "⑂ SWITCH / CASE";
};
LiteGraph.registerNodeType("hub/switch_case", NodeSwitchCase);