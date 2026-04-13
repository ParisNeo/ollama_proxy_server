function NodeFileReader() {
    this.addInput("Path Override", "string");
    this.addOutput("Content", "string");
    this.properties = { path: "" };
    
    this.pWidget = this.addWidget("text", "File Path", this.properties.path, (v) => { 
        this.properties.path = v; 
        if(window.pushHistoryState) window.pushHistoryState(); 
    });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "📄 FILE READER";
    this.color = "#ea580c";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeFileReader.title = "📄 FILE READER";
NodeFileReader.prototype.onConfigure = function() {
    if (this.pWidget) this.pWidget.value = this.properties.path;
    if (!this.title || this.title === "NodeFileReader") this.title = "📄 FILE READER";
};
LiteGraph.registerNodeType("hub/file_reader", NodeFileReader);

function NodeWebLoader() {
    this.addInput("URL Override", "string");
    this.addOutput("Text Content", "string");
    this.properties = { url: "" };
    
    this.uWidget = this.addWidget("text", "URL", this.properties.url, (v) => { 
        this.properties.url = v; 
        if(window.pushHistoryState) window.pushHistoryState(); 
    });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "🌐 URL SCRAPER";
    this.color = "#ea580c";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeWebLoader.title = "🌐 URL SCRAPER";
NodeWebLoader.prototype.onConfigure = function() {
    if (this.uWidget) this.uWidget.value = this.properties.url;
    if (!this.title || this.title === "NodeWebLoader") this.title = "🌐 URL SCRAPER";
};
LiteGraph.registerNodeType("hub/web_loader", NodeWebLoader);

function NodeDirScanner() {
    this.addOutput("File Paths", "array");
    this.properties = { path: "", extensions: ".py,.txt,.md" };
    
    this.pWidget = this.addWidget("text", "Folder Path", this.properties.path, (v) => { 
        this.properties.path = v; 
        if(window.pushHistoryState) window.pushHistoryState(); 
    });
    
    this.eWidget = this.addWidget("text", "Filter (csv)", this.properties.extensions, (v) => { 
        this.properties.extensions = v; 
        if(window.pushHistoryState) window.pushHistoryState(); 
    });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });

    this.title = "📁 DIRECTORY SCANNER";
    this.color = "#ea580c";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
NodeDirScanner.title = "📁 DIRECTORY SCANNER";
NodeDirScanner.prototype.onConfigure = function() {
    if (this.pWidget) this.pWidget.value = this.properties.path;
    if (this.eWidget) this.eWidget.value = this.properties.extensions;
    if (!this.title || this.title === "NodeDirScanner") this.title = "📁 DIRECTORY SCANNER";
};
LiteGraph.registerNodeType("hub/dir_scanner", NodeDirScanner);