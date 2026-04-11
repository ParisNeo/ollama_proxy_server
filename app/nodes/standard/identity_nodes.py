import re
from typing import Dict, Any
from app.nodes.base import BaseNode

class ModelSelectorNode(BaseNode):
    node_type = "hub/model"
    node_title = "Model Selector"
    node_category = "Selectors"
    node_icon = "🧠"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeModel() {
    this.addOutput("Expert", "expert");
    this.properties = { model: "auto" };
    this.addWidget("combo", "Target", this.properties.model, (v) => { this.properties.model = v; }, { values: window.available_models || ["auto"] });
    this.title = "🧠 MODEL SELECTOR";
    this.color = "#4338ca";
}
LiteGraph.registerNodeType("hub/model", NodeModel);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        return {"type": "expert_bundle", "model": node["properties"].get("model", "auto"), "persona": "", "skills": [], "temperature": None}

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