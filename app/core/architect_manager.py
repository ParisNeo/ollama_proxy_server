import json
import re
import secrets
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path
from app.core.events import event_manager, ProxyEvent
from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy

logger = logging.getLogger("architect")

class ArchitectObjective:
    TOOL = "tool"
    NODE = "node"
    WORKFLOW = "workflow"
    PERSONA = "persona"
    SKILL = "skill"
    CLUSTER = "cluster_config"

class MasterArchitect:
    """The central intelligence for all AI-assisted asset creation."""
    
    @staticmethod
    def get_instruction(objective: str, context_summary: str = "") -> str:
        base = (
            "You are the Master LoLLMs Architect. You generate high-quality system assets for an agentic cluster.\n"
            "STRICT PROTOCOL: Return ONLY a JSON object containing a 'manifest' and the 'content'.\n"
        )
        
        objectives = {
            "tool": "Output raw Python code using the 'lollms' host interface for per-user persistence. All functions start with 'tool_'.",
            "node": "Output a paired 'py_code' (BaseNode subclass) and 'js_code' (LiteGraph registration).",
            "workflow": "Output LiteGraph JSON. Ensure a 'hub/note' (ID 100) explains the logic flow.",
            "persona": "Output a Lollms Personality structure (YAML + Markdown Identity/Behaviour).",
            "skill": "Output a Claude-native SKILL.md format (YAML + Workflow logic).",
            "cluster_config": "Output a JSON array of model metadata updates. Match the provided Hub Model Names to the information in the context."
        }
        
        if objective == "cluster_config":
            return (
                f"{base}\n"
                "OBJECTIVE: Update model metadata based on external scorecard data.\n"
                "INPUT: A list of 'Hub Model Names' and a 'Scorecard Text'.\n"
                "TASK: Match each Hub Model to its scorecard equivalent. Extract max context size (in tokens), parameter count, vision support, and capability tags.\n\n"
                "MANDATORY JSON FORMAT:\n"
                "{\n"
                "  \"updates\": [\n"
                "    {\n"
                "      \"model_name\": \"exact_hub_name\",\n"
                "      \"max_context\": int,\n"
                "      \"supports_images\": bool,\n"
                "      \"is_reasoning\": bool,\n"
                "      \"is_code\": bool,\n"
                "      \"model_scale\": int (1:Small, 2:Medium, 3:Large),\n"
                "      \"model_size\": float (Parameters in Billions, e.g. 7.0, 70.0),\n"
                "      \"description\": \"One-sentence technical profile for the autorouter.\",\n"
                "      \"priority\": int (1-100)\n"
                "    }\n"
                "  ]\n"
                "}"
            )
        return f"{base}\nCURRENT OBJECTIVE: {objectives.get(objective, 'General creation')}\nCONTEXT: {context_summary}\n\nFORMAT:\n{{\"manifest\": {{...}}, \"content\": \"...\", \"filename\": \"...\"}}"

    @classmethod
    async def execute_build(cls, db, request, objective: str, prompt: str, context_files: str = "", sender: str = "admin") -> Dict[str, Any]:
        app_settings = request.app.state.settings
        target_agent = app_settings.admin_agent_name
        
        if not target_agent:
            raise ValueError("No Management Agent set in Settings.")

        build_id = f"sys_arch_{secrets.token_hex(4)}"
        event_manager.emit(ProxyEvent("received", build_id, f"Architect:{objective}", "Hub", sender, error_message=f"Initializing {objective} build..."))

        system_msg = cls.get_instruction(objective, context_files)
        user_payload = f"TASK: {prompt}\n\nREFERENCE DATA:\n{context_files}"

        # Resolve and Proxy
        res_model, msgs = await _resolve_target(db, target_agent, [{"role": "user", "content": user_payload}], request=request)
        msgs.insert(0, {"role": "system", "content": system_msg})

        from app.crud import server_crud
        servers = await server_crud.get_servers_with_model(db, res_model)
        
        event_manager.emit(ProxyEvent("active", build_id, f"Architect:{objective}", res_model, sender, error_message="AI is generating implementation..."))
        
        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": res_model, "messages": msgs, "stream": False}).encode(), is_subrequest=True, request_id=build_id, model=res_model, sender=sender)
        
        if not hasattr(resp, 'body'):
            raise Exception("Empty response from AI")

        data = json.loads(resp.body.decode())
        raw_output = data.get("message", {}).get("content", "").strip()
        
        # Clean and Parse
        clean_json = re.sub(r'```(?:json)?\s*([\s\S]*?)```', r'\1', raw_output).strip()
        parsed = json.loads(clean_json)
        
        event_manager.emit(ProxyEvent("completed", build_id, f"Architect:{objective}", "Local", sender, error_message=f"Deployment of {objective} complete!"))
        return parsed