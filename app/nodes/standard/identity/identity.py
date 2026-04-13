import re
from typing import Dict, Any
from app.nodes.base import BaseNode

class ExpertSelectorNode(BaseNode):
    node_type = "hub/expert"
    node_title = "Expert Selector"
    node_category = "Selectors"
    node_icon = "🎓"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        from app.core.personalities_manager import PersonalityManager
        props = node.get("properties", {})
        persona_text = ""
        if props.get("personality"):
            p = next((x for x in PersonalityManager.get_all_personalities() if x["name"] == props["personality"]), None)
            if p: persona_text = re.sub(r'^---\n.*?\n---\n', '', p["raw"], flags=re.DOTALL).strip()

        # Ensure we pass the name for the engine to know which persona to resolve
        return {
            "type": "expert_bundle",
            "model": props.get("model", "auto"),
            "personality_name": props.get("personality"), 
            "personality": persona_text,
            "skills": [],
            "tools": [],
            "temperature": props.get("temperature", 0.7)
        }

class PersonalityNode(BaseNode):
    node_type = "hub/personality"
    node_title = "Personality"
    node_category = "Serving & Cognition"
    node_icon = "🎭"

    async def execute(self, engine, node, output_slot_idx):
        from app.core.personalities_manager import PersonalityManager
        p_name = node["properties"].get("name")
        p = next((x for x in PersonalityManager.get_all_personalities() if x["name"] == p_name), None)
        return re.sub(r'^---\n.*?\n---\n', '', p["raw"], flags=re.DOTALL).strip() if p else ""

class SkillNode(BaseNode):
    node_type = "hub/skill"
    node_title = "Skill Selector"
    node_category = "Selectors"
    node_icon = "📜"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        from app.core.skills_manager import SkillsManager
        s_name = node["properties"].get("name")
        skill = next((s for s in SkillsManager.get_all_skills() if s["name"] == s_name), None)
        return skill["raw"] if skill else ""