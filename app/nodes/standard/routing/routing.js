function NodeAutoRouter() {
    this.addInput("User Context", "messages");
    this.addInput("Expert 1", "expert,string");
    this.addOutput("Route Output", "string");
    
    this.addWidget("button", "Configure Rules", null, () => { window.openRouterModal(this); });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    
    this.title = "🔀 AUTO ROUTER";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeAutoRouter.title = "🔀 AUTO ROUTER";
NodeAutoRouter.prototype.onConfigure = function() {
    if (!this.title || this.title === "NodeAutoRouter") this.title = "🔀 AUTO ROUTER";
};
LiteGraph.registerNodeType("hub/autorouter", NodeAutoRouter);

function NodeMoE() {
    this.addInput("User Context", "messages");
    this.addInput("Expert 1", "expert,string");
    this.addOutput("Final Output", "string");
    this.properties = { 
        orchestrator: "auto",
        persona: "",
        show_intermediate: true,
        send_status: true,
        system_prompt: "Combine the ideas from the experts into a single high-quality response."
    };
    
    this.mWidget = this.addWidget("combo", "Orchestrator", this.properties.orchestrator, (v) => { 
        this.properties.orchestrator = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: ["auto"].concat(window.available_models) });

    this.pWidget = this.addWidget("combo", "Personality", this.properties.persona, (v) => { 
        this.properties.persona = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: [""].concat(window.logic_blocks || []) });

    this.sWidget = this.addWidget("toggle", "Show Experts", this.properties.show_intermediate, (v) => {
        this.properties.show_intermediate = v;
        if(window.pushHistoryState) window.pushHistoryState();
    });

    this.stWidget = this.addWidget("toggle", "Send Status", this.properties.send_status, (v) => {
        this.properties.send_status = v;
        if(window.pushHistoryState) window.pushHistoryState();
    });
    
    this.addWidget("button", "+ Add Expert", null, () => {
        this.addInput("Expert " + (this.inputs.length), "expert,string");
        this.size = this.computeSize();
        if(window.pushHistoryState) window.pushHistoryState();
    });

    this.addWidget("button", "Edit Prompt", null, () => {
        const val = prompt("Enter Orchestrator Prompt:", this.properties.system_prompt);
        if (val !== null) this.properties.system_prompt = val;
    });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "✨ MIXTURE OF EXPERTS";
    this.color = "#7c3aed";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeMoE.title = "✨ MIXTURE OF EXPERTS";
NodeMoE.prototype.onConfigure = function(config) {
    if (this.mWidget) this.mWidget.value = this.properties.orchestrator;
    if (this.pWidget) this.pWidget.value = this.properties.persona;
    if (this.sWidget) this.sWidget.value = this.properties.show_intermediate;
    if (this.stWidget) this.stWidget.value = this.properties.send_status;
    this.size = this.computeSize();
};
LiteGraph.registerNodeType("hub/moe", NodeMoE);