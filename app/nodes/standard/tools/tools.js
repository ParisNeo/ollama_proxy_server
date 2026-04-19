function NodeToolSelector() {
    this.addOutput("Tool Schema", "tool");
    this.properties = { library: "", function: "" };
    
    this.lWidget = this.addWidget("combo", "Library", this.properties.library, (v) => { 
        this.properties.library = v; 
        this.refreshFunctions();
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values:[] });
    
    this.fWidget = this.addWidget("combo", "Function", this.properties.function, (v) => { 
        this.properties.function = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values:[] });

    this.confBtn = this.addWidget("button", "⚙️ Configure Tool", null, () => { 
        window.openToolConfigWizard(this);
    });
    this.confBtn.disabled = true;

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "🛠️ TOOL SELECTOR";
    this.color = "#9f1239";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeToolSelector.title = "🛠️ TOOL SELECTOR";
NodeToolSelector.prototype.onConfigure = function() {
    if (this.lWidget) this.lWidget.value = this.properties.library;
    if (this.fWidget) this.fWidget.value = this.properties.function;
    if (!this.title || this.title === "NodeToolSelector") this.title = "🛠️ TOOL SELECTOR";
};
NodeToolSelector.prototype.onAdded = async function() {
    try {
        const resp = await fetch("/api/v1/api/tools");
        this.allToolsData = await resp.json();
        this.lWidget.options.values = this.allToolsData.map(t => t.filename);
        if (this.properties.library) this.refreshFunctions(false);
    } catch(e) { console.error("Tool fetch failed", e); }
};
NodeToolSelector.prototype.refreshFunctions = function(resetSelection = true) {
    const lib = this.allToolsData?.find(t => t.filename === this.properties.library);
    if (lib) {
        // --- NEW: Handle Config Metadata ---
        // We use a simplified regex-based metadata check to update the UI button immediately
        const hasMeta = lib.raw.includes("TOOL_SETTINGS_METADATA");
        this.confBtn.disabled = !hasMeta;
        this.confBtn.name = hasMeta ? "⚙️ Configure Tool" : "No Config Required";

        const matches = [...lib.raw.matchAll(/def (tool_[\w_]+)/g)];
        const fns = ["[ALL FUNCTIONS]"].concat(matches.map(m => m[1]));
        this.fWidget.options.values = fns;
        if (resetSelection && fns.length > 0) {
            this.properties.function = fns[0];
            this.fWidget.value = fns[0];
        }
    } else {
        this.fWidget.options.values =[];
    }
};
// --- NEW: Tool Configuration Wizard ---
window.openToolConfigWizard = async (node) => {
    const lib = node.allToolsData?.find(t => t.filename === node.properties.library);
    if (!lib) return;

    // Use a fresh fetch to get the parsed metadata including settings_metadata
    window.showModal("Configuring Tool", '<div class="p-10 text-center animate-pulse">Parsing Tool Requirements...</div>');
    
    // For simplicity, we parse the metadata from the raw code locally
    const metaMatch = lib.raw.match(/TOOL_SETTINGS_METADATA\s*=\s*(\[[\s\S]*?\])/);
    if (!metaMatch) {
        window.showModal("Info", "This tool does not require design-time configuration.");
        return;
    }

    let schema = [];
    try {
        // Use a safe JSON-like parse for the simple list defined in Python
        // This handles standard key-value patterns in the TOOL_SETTINGS_METADATA variable
        const rawList = metaMatch[1].replace(/'/g, '"');
        schema = JSON.parse(rawList);
    } catch(e) {
        console.error("Failed to parse tool metadata", e);
        window.showModal("Error", "The tool library has malformed TOOL_SETTINGS_METADATA.");
        return;
    }

    if (!node.properties.tool_settings) node.properties.tool_settings = {};
    const current = node.properties.tool_settings;

    let html = `<div class="space-y-6 text-left">
        <div class="p-3 bg-sky-500/10 border border-sky-500/30 rounded-xl mb-4 flex items-center gap-3">
            <span class="text-xl">${lib.icon || '🔧'}</span>
            <div>
                <div class="text-xs font-black text-white uppercase">${lib.name} Configuration</div>
                <p class="text-[10px] text-gray-500">Configure credentials or static settings for this tool instance.</p>
            </div>
        </div>
        <div class="space-y-4">`;

    schema.forEach(field => {
        const val = current[field.name] !== undefined ? current[field.name] : (field.default || "");
        const inputType = field.type === 'password' ? 'password' : (field.type === 'number' ? 'number' : 'text');
        
        html += `
            <div>
                <label class="block text-[10px] font-black text-gray-400 uppercase tracking-widest mb-1">${field.name}</label>
                <input type="${inputType}" id="conf-${field.name}" value="${val}" class="w-full bg-black/40 border border-white/10 rounded-lg p-2.5 text-sm text-indigo-300 outline-none focus:border-sky-500">
                <p class="text-[9px] text-gray-600 mt-1 italic">${field.description || ""}</p>
            </div>
        `;
    });

    html += `</div>
        <div class="flex justify-end pt-4 border-t border-white/5 mt-6">
            <button onclick="saveToolConfig(${node.id}, ${JSON.stringify(schema).replace(/"/g, '&quot;')})" class="bg-sky-600 hover:bg-sky-500 text-white px-10 py-2 rounded-xl font-black text-xs uppercase tracking-widest transition-all shadow-lg">Apply Configuration</button>
        </div>
    </div>`;

    window.showModal("Tool Instance Configuration", html);
};

window.saveToolConfig = (nodeId, schema) => {
    const node = graph.getNodeById(nodeId);
    const settings = {};
    schema.forEach(field => {
        const input = document.getElementById(`conf-${field.name}`);
        if (input) settings[field.name] = input.value;
    });
    node.properties.tool_settings = settings;
    document.getElementById('modal-close-btn').click();
    if(window.pushHistoryState) window.pushHistoryState();
    window.showToast("Tool configuration applied.", "success");
};

LiteGraph.registerNodeType("hub/tool_selector", NodeToolSelector);