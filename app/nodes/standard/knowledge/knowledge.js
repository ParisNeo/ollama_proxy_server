function NodeDatastore() {
    this.addInput("Query", "string");
    this.addOutput("Context", "string");
    this.properties = { datastore: "", top_k: 3 };
    
    this.dsWidget = this.addWidget("combo", "Store", this.properties.datastore, (v) => { 
        this.properties.datastore = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: window.datastores_list || [] });

    this.kWidget = this.addWidget("number", "Top K", this.properties.top_k, (v) => { 
        this.properties.top_k = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { min: 1, max: 20 });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "📚 RAG DATASTORE";
    this.color = "#0d9488";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeDatastore.title = "📚 RAG DATASTORE";
NodeDatastore.prototype.onConfigure = function(config) {
    if (this.dsWidget) this.dsWidget.value = this.properties.datastore;
    if (this.kWidget) this.kWidget.value = this.properties.top_k;
    if (!this.title || this.title === "NodeDatastore") this.title = "📚 RAG DATASTORE";
};
LiteGraph.registerNodeType("hub/datastore", NodeDatastore);

function NodeWebSearch() {
    this.addInput("Query", "string");
    this.addOutput("Results", "string");
    this.properties = { service: "wikipedia", max_results: 5 };
    
    this.sWidget = this.addWidget("combo", "Service", this.properties.service, (v) => { 
        this.properties.service = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: ["wikipedia", "arxiv", "google"] });
    
    this.mWidget = this.addWidget("number", "Max Results", this.properties.max_results, (v) => { 
        this.properties.max_results = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    }, { min: 1, max: 10 });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "🌐 WEB SEARCH";
    this.color = "#3b82f6";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeWebSearch.title = "🌐 WEB SEARCH";
NodeWebSearch.prototype.onConfigure = function() {
    if (this.sWidget) this.sWidget.value = this.properties.service;
    if (this.mWidget) this.mWidget.value = this.properties.max_results;
    if (!this.title || this.title === "NodeWebSearch") this.title = "🌐 WEB SEARCH";
};
LiteGraph.registerNodeType("hub/web_search", NodeWebSearch);