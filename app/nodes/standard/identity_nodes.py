import re
from typing import Dict, Any
from app.nodes.base import BaseNode

class ExpertSelectorNode(BaseNode):
    node_type = "hub/expert"
    node_title = "Expert Selector"
    node_category = "Selectors"
    node_icon = "🎓"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeExpert() {
    this.addOutput("Expert", "expert");
    this.properties = { 
        model: "auto",
        personality: "",
        skills: [],
        tools: [],
        temperature: 0.7
    };
    
    this.mWidget = this.addWidget("combo", "Model", this.properties.model, (v) => { this.properties.model = v; }, { values: ["auto"].concat(window.available_models) });
    this.pWidget = this.addWidget("combo", "Personality", this.properties.personality, (v) => { this.properties.personality = v; }, { values: [""].concat(window.logic_blocks) });
    this.addWidget("number", "Temp", 0.7, (v) => { this.properties.temperature = v; }, { min: 0, max: 2, step: 0.1 });
    
    this.addWidget("button", "+ Add Skill", null, () => {
        this.addInput("Skill " + (this.inputs ? this.inputs.length + 1 : 1), "skill,string");
        this.size = this.computeSize();
    });

    this.addWidget("button", "+ Add Tool", null, () => {
        this.addInput("Tool " + (this.inputs ? this.inputs.length + 1 : 1), "tool,mcp");
        this.size = this.computeSize();
    });

    this.title = "🎓 EXPERT BUILDER";
    this.color = "#4338ca";
    this.bgcolor = "#1e1b4b";
    this.size = [280, 160];
    this.serialize_widgets = true;
}

NodeExpert.prototype.onConfigure = function() {
    if(this.mWidget) this.mWidget.value = this.properties.model;
    if(this.pWidget) this.pWidget.value = this.properties.personality;
};

LiteGraph.registerNodeType("hub/expert", NodeExpert);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        from app.core.personalities_manager import PersonalityManager
        import re

        props = node.get("properties", {})
        
        # 1. Resolve Personality
        persona_text = ""
        if props.get("personality"):
            p = next((x for x in PersonalityManager.get_all_personalities() if x["name"] == props["personality"]), None)
            if p:
                persona_text = re.sub(r'^---\n.*?\n---\n', '', p["raw"], flags=re.DOTALL).strip()

        # 2. Resolve Skills and Tools from inputs
        skills = []
        tools = []
        for i in range(len(node.get("inputs", []))):
            val = await engine._resolve_input(node, i)
            if not val: continue
            
            # Simple heuristic: if it looks like a tool schema (dict with 'function'), it's a tool
            if isinstance(val, dict) and (val.get("type") == "function" or val.get("type") == "mcp"):
                tools.append(val)
            elif isinstance(val, list):
                 # Handle list of tools
                 if val and isinstance(val[0], dict) and val[0].get("type") in ("function", "mcp"):
                     tools.extend(val)
                 else:
                     skills.append(str(val))
            else:
                skills.append(str(val))

        return {
            "type": "expert_bundle",
            "model": props.get("model", "auto"),
            "personality": persona_text,
            "skills": skills,
            "tools": tools,
            "temperature": props.get("temperature", 0.7)
        }

class PersonalityNode(BaseNode):
    node_type = "hub/personality"
    node_title = "Personality"
    node_category = "Serving & Cognition"
    node_icon = "🎭"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodePersonality() {
    this.addOutput("System Prompt", "string");
    this.properties = { name: "" };
    this.addWidget("combo", "Persona", this.properties.name, (v) => { 
        this.properties.name = v;
        if(window.pushHistoryState) window.pushHistoryState();
    }, { values: window.logic_blocks || [] });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "🎭 PERSONALITY";
    this.color = "#86198f";
    this.serialize_widgets = true;
    this.size = this.computeSize();
}
LiteGraph.registerNodeType("hub/personality", NodePersonality);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        from app.core.personalities_manager import PersonalityManager
        p_name = node["properties"].get("name")
        p = next((x for x in PersonalityManager.get_all_personalities() if x["name"] == p_name), None)
        if not p: return ""
        return re.sub(r'^---\n.*?\n---\n', '', p["raw"], flags=re.DOTALL).strip()

class SkillNode(BaseNode):
    node_type = "hub/skill"
    node_title = "Skill"
    node_category = "Selectors"
    node_icon = "📜"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeSkill() {
    this.addOutput("Skill", "skill");
    this.properties = { name: "" };
    this.addWidget("combo", "Skill", this.properties.name, (v) => { this.properties.name = v; });
    this.title = "📜 SKILL";
}
LiteGraph.registerNodeType("hub/skill", NodeSkill);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        from app.core.skills_manager import SkillsManager
        s_name = node["properties"].get("name")
        skill = next((s for s in SkillsManager.get_all_skills() if s["name"] == s_name), None)
        return skill["raw"] if skill else ""