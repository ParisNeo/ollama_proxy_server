function NodeExpert() {
    this.addOutput("Expert", "expert");
    this.properties = { model: "auto", personality: "", temperature: 0.7 };
    
    // Maintain references to widgets to update them in onConfigure
    this.mWidget = this.addWidget("combo", "Model", this.properties.model, (v) => { 
        this.properties.model = v; 
        if(window.pushHistoryState) window.pushHistoryState(); 
    }, { values: ["auto"].concat(window.available_models) });

    this.pWidget = this.addWidget("combo", "Personality", this.properties.personality, (v) => { 
        this.properties.personality = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: [""].concat(window.logic_blocks) });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "🎓 EXPERT BUILDER";
    this.color = "#4338ca";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeExpert.title = "🎓 EXPERT BUILDER";
NodeExpert.prototype.onConfigure = function(config) {
    if (this.mWidget) this.mWidget.value = this.properties.model;
    if (this.pWidget) this.pWidget.value = this.properties.personality;
    if (!this.title || this.title === "NodeExpert") this.title = "🎓 EXPERT BUILDER";
    this.size = this.computeSize(); // RE-CALCULATE
};

NodeExpert.prototype.onAdded = function() {
    this.size = this.computeSize(); // RE-CALCULATE
};

// Force update on any change to the graph
NodeExpert.prototype.onAction = function() {
    this.size = this.computeSize();
};

LiteGraph.registerNodeType("hub/expert", NodeExpert);

function NodePersonality() {
    this.addOutput("System Prompt", "string");
    this.properties = { name: "" };
    this.pWidget = this.addWidget("combo", "Persona", this.properties.name, (v) => { 
        this.properties.name = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: window.logic_blocks || [] });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "🎭 PERSONALITY";
    this.color = "#86198f";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodePersonality.title = "🎭 PERSONALITY";
NodePersonality.prototype.onConfigure = function(config) {
    if (this.pWidget) this.pWidget.value = this.properties.name;
    if (!this.title || this.title === "NodePersonality") this.title = "🎭 PERSONALITY";
};
LiteGraph.registerNodeType("hub/personality", NodePersonality);

function NodeSkill() {
    this.addOutput("Skill Content", "string");
    this.properties = { name: "" };
    this.sWidget = this.addWidget("combo", "Skill", this.properties.name, (v) => { 
        this.properties.name = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: window.SERVER_SKILLS ? window.SERVER_SKILLS.map(s => s.name) : [] });
    
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "📜 SKILL SELECTOR";
    this.color = "#4f46e5";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeSkill.title = "📜 SKILL SELECTOR";
NodeSkill.prototype.onConfigure = function(config) {
    if (this.sWidget) this.sWidget.value = this.properties.name;
    if (!this.title || this.title === "NodeSkill") this.title = "📜 SKILL SELECTOR";
};
NodeSkill.prototype.onAdded = function() {
    if (window.SERVER_SKILLS) {
        this.sWidget.options.values = window.SERVER_SKILLS.map(s => s.name);
    }
};
LiteGraph.registerNodeType("hub/skill", NodeSkill);