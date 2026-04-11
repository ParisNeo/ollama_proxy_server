from typing import Dict, Any
from app.nodes.base import BaseNode

class SystemModifierNode(BaseNode):
    node_type = "hub/system_modifier"
    node_title = "System Modifier"
    node_category = "Logic & Routing"
    node_icon = "⚡"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeSystemModifier() {
    this.addInput("Messages", "messages");
    this.addInput("System Prompt", "string");
    this.addOutput("Updated Messages", "messages");
    this.properties = { replace_all: false };
    this.addWidget("toggle", "Replace Existing", false, (v) => { 
        this.properties.replace_all = v; 
        if(window.pushHistoryState) window.pushHistoryState();
    });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "⚡ SYSTEM MODIFIER";
    this.color = "#2563eb";
    this.size = this.computeSize();
    this.serialize_widgets = true;
}
LiteGraph.registerNodeType("hub/system_modifier", NodeSystemModifier);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        history = await engine._resolve_input(node, 0)
        if history is None:
            history = engine.initial_messages
        
        import copy
        # Deep copy to avoid mutating shared state or initial_messages
        history = copy.deepcopy(history)
        
        sys_prompt_text = await engine._resolve_input(node, 1)
        if not sys_prompt_text:
            return history
            
        replace_all = node["properties"].get("replace_all", True)
        
        if not isinstance(history, list):
            history = [{"role": "user", "content": str(history)}]

        if replace_all:
            # Remove all existing system messages and insert fresh one at top
            updated = [m for m in history if m.get("role") != "system"]
            updated.insert(0, {"role": "system", "content": str(sys_prompt_text)})
            return updated
        else:
            # Find the first system message and append to it
            system_msg = next((m for m in history if m.get("role") == "system"), None)
            if system_msg:
                current_content = system_msg.get("content", "")
                system_msg["content"] = f"{current_content}\n\n{sys_prompt_text}".strip()
                return history
            else:
                # No existing system message to append to, insert as new
                history.insert(0, {"role": "system", "content": str(sys_prompt_text)})
                return history

class SystemComposerNode(BaseNode):
    node_type = "hub/system_composer"
    node_title = "System Composer"
    node_category = "Logic & Routing"
    node_icon = "🏗️"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeSystemComposer() {
    this.addInput("Persona", "string");
    this.addInput("Skill 1", "string");
    this.addInput("RAG Context", "string");
    this.addOutput("System String", "string");
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "🏗️ SYSTEM COMPOSER";
    this.color = "#7c3aed";
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/system_composer", NodeSystemComposer);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        parts = []
        for i in range(len(node.get("inputs", []))):
            val = await engine._resolve_input(node, i)
            if val: parts.append(str(val))
        return "\n\n".join(parts)

class CreateMessageNode(BaseNode):
    node_type = "hub/create_message"
    node_title = "Create Message"
    node_category = "Logic & Routing"
    node_icon = "✉️"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeCreateMessage() {
    this.addInput("Text", "string");
    this.addOutput("Message", "message_obj");
    this.properties = { role: "user" };
    this.addWidget("combo", "Role", this.properties.role, (v) => { this.properties.role = v; }, { values:["user", "assistant", "system"] });
    this.title = "✉️ CREATE MESSAGE";
}
LiteGraph.registerNodeType("hub/create_message", NodeCreateMessage);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        text = await engine._resolve_input(node, 0)
        return {"role": node["properties"].get("role", "user"), "content": str(text)} if text else None