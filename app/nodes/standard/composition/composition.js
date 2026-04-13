function NodeSystemModifier() {
    this.addInput("Messages", "messages");
    this.addInput("System Prompt", "string");
    this.addOutput("Updated Messages", "messages");
    this.properties = { replace_all: false };
    
    this.tWidget = this.addWidget("toggle", "Replace Existing", this.properties.replace_all, (v) => { 
        this.properties.replace_all = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "⚡ SYSTEM MODIFIER";
    this.color = "#2563eb";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeSystemModifier.title = "⚡ SYSTEM MODIFIER";
NodeSystemModifier.prototype.onConfigure = function() {
    if (this.tWidget) this.tWidget.value = this.properties.replace_all;
    if (!this.title || this.title === "NodeSystemModifier") this.title = "⚡ SYSTEM MODIFIER";
};
LiteGraph.registerNodeType("hub/system_modifier", NodeSystemModifier);

function NodeSystemComposer() {
    this.addInput("Persona", "string");
    this.addInput("Skill 1", "string");
    this.addInput("RAG Context", "string");
    this.addOutput("System String", "string");
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "🏗️ SYSTEM COMPOSER";
    this.color = "#7c3aed";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeSystemComposer.title = "🏗️ SYSTEM COMPOSER";
NodeSystemComposer.prototype.onConfigure = function() {
    if (!this.title || this.title === "NodeSystemComposer") this.title = "🏗️ SYSTEM COMPOSER";
};
LiteGraph.registerNodeType("hub/system_composer", NodeSystemComposer);