function NodeAutoRouter() {
    this.addInput("Messages", "messages");
    this.addOutput("Model Name", "string");
    this.properties = {
        mode: "rules", // "rules" or "semantic"
        rules: [],
        candidate_models: ["auto"],
        classifier_model: "auto",
        default_model: "auto"
    };

    this.addWidget("combo", "Routing Mode", this.properties.mode, (v) => {
        this.properties.mode = v;
    }, { values: ["rules", "semantic"] });

    this.addWidget("button", "Open Router Wizard", null, () => {
        window.openRouterWizard(this);
    });

    this.title = "🚦 SMART ROUTER";
    this.color = "#1e293b"; // Charcoal
    this.size = [250, 100];
    this.serialize_widgets = true;
}

NodeAutoRouter.title = "🚦 SMART ROUTER";

// Helper to generate model options from global list
const generateModelOptions = (selected) => {
    const models = window.available_models || ["auto"];
    return models.map(m => `<option value="${m}" ${m === selected ? 'selected' : ''}>${m}</option>`).join('');
};

// Custom Wizard for Configuration
window.openRouterWizard = (node) => {
    const p = node.properties;
    const modelOptions = generateModelOptions();
    
    let html = `
        <div class="space-y-6 text-left max-h-[75vh] overflow-y-auto custom-scrollbar pr-2">
            <div class="bg-indigo-500/10 border border-indigo-500/20 p-4 rounded-xl">
                <h3 class="text-xs font-black text-indigo-400 uppercase tracking-widest mb-2">1. Candidate Models Pool</h3>
                <p class="text-[10px] text-gray-500 mb-3">Define the list of models this router is allowed to return.</p>
                <div id="model-pool" class="space-y-2">
                    ${(p.candidate_models || []).map((m, i) => `
                        <div class="flex gap-2">
                            <select class="model-entry flex-grow bg-black/40 border border-white/10 rounded px-2 py-1 text-xs">
                                ${generateModelOptions(m)}
                            </select>
                            <button onclick="this.parentElement.remove()" class="text-red-400">&times;</button>
                        </div>
                    `).join('')}
                </div>
                <button onclick="addModelToPool()" class="mt-2 text-[10px] text-indigo-400 font-bold uppercase">+ Add Model</button>
            </div>

            <div class="bg-black/40 border border-white/10 p-4 rounded-xl">
                <h3 class="text-xs font-black text-white uppercase tracking-widest mb-2">2. Decision Engine</h3>
                <div id="rules-view" class="${p.mode === 'rules' ? '' : 'hidden'} space-y-4">
                    <p class="text-[10px] text-gray-500 mb-2">Rules are checked in order. First match wins.</p>
                    <div id="rules-list" class="space-y-2">
                        ${(p.rules || []).map((r, i) => `
                            <div class="rule-group p-3 bg-white/5 rounded-lg border border-white/5 space-y-2">
                                <div class="flex justify-between items-center">
                                    <span class="text-[10px] font-bold text-gray-400">RULE #${i+1}</span>
                                    <button onclick="this.parentElement.parentElement.remove()" class="text-red-500">&times;</button>
                                </div>
                                <div class="grid grid-cols-2 gap-2">
                                    <select class="rule-type text-[10px] bg-black/60 border border-white/10 rounded">
                                        <option value="keyword" ${r.type === 'keyword' ? 'selected' : ''}>Contains Keyword</option>
                                        <option value="regex" ${r.type === 'regex' ? 'selected' : ''}>Regex Match</option>
                                        <option value="min_len" ${r.type === 'min_len' ? 'selected' : ''}>Min Length</option>
                                        <option value="max_len" ${r.type === 'max_len' ? 'selected' : ''}>Max Length</option>
                                    </select>
                                    <input type="text" class="rule-val text-[10px] bg-black/60 border border-white/10 rounded px-2" value="${r.value}" placeholder="Value">
                                </div>
                                <div class="flex items-center gap-2 pt-1">
                                    <span class="text-[9px] uppercase text-gray-600 font-bold">Target Model:</span>
                                    <select class="rule-target text-[10px] bg-indigo-500/10 border border-indigo-500/20 rounded px-2 flex-grow">
                                        ${generateModelOptions(r.target)}
                                    </select>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                    <button onclick="addRuleToWizard()" class="text-[10px] text-emerald-400 font-bold uppercase">+ Add Firewall Rule</button>
                </div>

                <div id="semantic-view" class="${p.mode === 'semantic' ? '' : 'hidden'} space-y-4">
                    <div class="flex flex-col gap-2">
                        <label class="text-[10px] font-bold text-purple-400 uppercase">Classifier Model (Small/Fast)</label>
                        <input type="text" id="wiz-classifier" value="${p.classifier_model}" class="bg-black/60 border border-white/10 rounded px-2 py-1 text-xs">
                    </div>
                    <p class="text-[10px] text-gray-500">The router will ask the classifier to pick the best model from your pool based on user intent.</p>
                </div>
            </div>

            <div class="flex justify-end pt-4">
                <button onclick="saveRouterWizard(${node.id})" class="bg-indigo-600 hover:bg-indigo-500 px-10 py-2 rounded font-black text-xs uppercase tracking-widest text-white shadow-lg">Save Configuration</button>
            </div>
        </div>
    `;

    window.showModal("Router Configuration (Firewall vs Semantic)", html);
};

window.addModelToPool = () => {
    const div = document.createElement('div');
    div.className = "flex gap-2";
    div.innerHTML = `
        <select class="model-entry flex-grow bg-black/40 border border-white/10 rounded px-2 py-1 text-xs">
            ${generateModelOptions()}
        </select>
        <button onclick="this.parentElement.remove()" class="text-red-400">&times;</button>
    `;
    document.getElementById('model-pool').appendChild(div);
};

window.addRuleToWizard = () => {
    const div = document.createElement('div');
    div.className = "rule-group p-3 bg-white/5 rounded-lg border border-white/5 space-y-2";
    div.innerHTML = `
        <div class="flex justify-between items-center">
            <span class="text-[10px] font-bold text-gray-400">NEW RULE</span>
            <button onclick="this.parentElement.parentElement.remove()" class="text-red-500">&times;</button>
        </div>
        <div class="grid grid-cols-2 gap-2">
            <select class="rule-type text-[10px] bg-black/60 border border-white/10 rounded">
                <option value="keyword">Contains Keyword</option>
                <option value="regex">Regex Match</option>
                <option value="min_len">Min Length</option>
                <option value="max_len">Max Length</option>
            </select>
            <input type="text" class="rule-val text-[10px] bg-black/60 border border-white/10 rounded px-2" placeholder="Value">
        </div>
        <div class="flex items-center gap-2 pt-1">
            <span class="text-[9px] uppercase text-gray-600 font-bold">Target Model:</span>
            <select class="rule-target text-[10px] bg-indigo-500/10 border border-indigo-500/20 rounded px-2 flex-grow">
                ${generateModelOptions()}
            </select>
        </div>
    `;
    document.getElementById('rules-list').appendChild(div);
};

window.saveRouterWizard = (nodeId) => {
    const node = graph.getNodeById(nodeId);
    const pool = Array.from(document.querySelectorAll('.model-entry')).map(i => i.value).filter(v => v);
    const rules = Array.from(document.querySelectorAll('.rule-group')).map(el => ({
        type: el.querySelector('.rule-type').value,
        value: el.querySelector('.rule-val').value,
        target: el.querySelector('.rule-target').value
    }));

    node.properties.candidate_models = pool;
    node.properties.rules = rules;
    node.properties.classifier_model = document.getElementById('wiz-classifier').value;
    
    document.getElementById('modal-close-btn').click();
    if(window.pushHistoryState) window.pushHistoryState();
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