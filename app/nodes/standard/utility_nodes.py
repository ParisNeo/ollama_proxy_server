from typing import Dict, Any
from app.nodes.base import BaseNode

class NoteNode(BaseNode):
    node_type = "hub/note"
    node_title = "Markdown Note"
    node_category = "Utility"
    node_icon = "📝"

    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeNote() {
    this.properties = { content: "# Project Instruction\\nAdd details about this graph here..." };
    
    this.addWidget("button", "Edit Markdown", null, () => {
        const modalBody = `
            <div class="space-y-4 text-left flex flex-col h-[60vh]">
                <textarea id="note-editor" class="flex-grow w-full bg-black/40 border border-white/10 rounded-lg p-4 font-mono text-sm text-gray-200 outline-none focus:border-indigo-500 custom-scrollbar resize-none">${this.properties.content}</textarea>
                <div class="flex justify-end pt-4 border-t border-white/10">
                    <button type="button" id="save-note-btn" class="bg-indigo-600 hover:bg-indigo-500 px-10 py-2 rounded font-black text-xs uppercase tracking-widest text-white shadow-lg transition-all">Update Note</button>
                </div>
            </div>
        `;
        window.showModal("Edit Markdown Note", modalBody);
        
        document.getElementById('save-note-btn').onclick = () => {
            this.properties.content = document.getElementById('note-editor').value;
            this.fitToContent();
            this.setDirtyCanvas(true, true);
            document.getElementById('modal-close-btn').click();
            if(window.pushHistoryState) window.pushHistoryState();
        };
    });

    this.title = "📝 NOTE";
    this.color = "#854d0e"; 
    this.bgcolor = "#fef9c3"; 
    this.boxcolor = "#000";
    this.size = [450, 250];
    this.serialize_widgets = true;
}

NodeNote.prototype.onPropertyChanged = function(name, value) {
    if (name === "content") {
        this.fitToContent();
    }
    return true;
};

// Logic to resize the node based on text lines
NodeNote.prototype.fitToContent = function() {
    const width = this.size[0] || 450;
    const padding = 20;
    const maxWidth = width - (padding * 2);
    
    const content = (this.properties.content || "").replace(/\\n/g, "\\n");
    const lines = content.split("\\n");
    
    let totalHeight = 100; // Corrected base offset for header + widget
    
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    
    lines.forEach(line => {
        let fontSize = 14;
        let textToMeasure = line;
        
        if(line.startsWith("# ")) { fontSize = 22; textToMeasure = line.substring(2); }
        else if(line.startsWith("## ")) { fontSize = 18; textToMeasure = line.substring(3); }
        
        ctx.font = fontSize + "px sans-serif";
        const words = textToMeasure.split(" ");
        let currentLine = "";
        let lineCount = 1;
        
        for(let n = 0; n < words.length; n++) {
            let testLine = currentLine + words[n] + " ";
            if (ctx.measureText(testLine).width > maxWidth && n > 0) {
                lineCount++;
                currentLine = words[n] + " ";
            } else {
                currentLine = testLine;
            }
        }
        totalHeight += (lineCount * (fontSize + 6)) + 8;
    });
    
    this.size[1] = Math.max(150, totalHeight);
};

NodeNote.prototype.onDrawForeground = function(ctx) {
    if (this.flags.collapsed) return;
    
    const padding = 20;
    const maxWidth = this.size[0] - (padding * 2);
    let y = 100;

    const content = (this.properties.content || "").replace(/\\n/g, "\\n");
    const lines = content.split("\\n");

    lines.forEach(line => {
        let fontSize = 14;
        let isHeader = false;
        let color = "#451a03";
        let textToDraw = line;

        if(line.startsWith("# ")) { fontSize = 22; isHeader = true; textToDraw = line.substring(2); color = "#1a0f00"; }
        else if(line.startsWith("## ")) { fontSize = 18; isHeader = true; textToDraw = line.substring(3); color = "#2b1b00"; }
        else if(line.startsWith("- ")) { textToDraw = "• " + line.substring(2); }

        ctx.fillStyle = color;
        ctx.font = (isHeader ? "bold " : "") + fontSize + "px sans-serif";

        const words = textToDraw.split(" ");
        let currentLine = "";
        
        for(let n = 0; n < words.length; n++) {
            let testLine = currentLine + words[n] + " ";
            if (ctx.measureText(testLine).width > maxWidth && n > 0) {
                ctx.fillText(currentLine.trim(), padding, y + fontSize);
                y += fontSize + 6;
                currentLine = words[n] + " ";
            } else {
                currentLine = testLine;
            }
        }
        
        ctx.fillText(currentLine.trim(), padding, y + fontSize);
        y += fontSize + 12;
    });
};

NodeNote.prototype.onAdded = function() {
    this.fitToContent();
};

LiteGraph.registerNodeType("hub/note", NodeNote);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        # Notes are visual only, but can pass their content if connected
        return node["properties"].get("content", "")