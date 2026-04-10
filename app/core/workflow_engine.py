import json
import logging
import asyncio
import re
from typing import Dict, Any, List, Tuple
from sqlalchemy import select
from fastapi import Request

from app.crud import server_crud
from app.core.events import event_manager, ProxyEvent
from app.nodes.registry import NodeRegistry

logger = logging.getLogger(__name__)

class WorkflowEngine:
    def __init__(self, db, request: Request, request_id: str, sender: str, name: str, graph_data: Dict[str, Any], depth: int, reverse_proxy_fn, resolve_target_fn):
        self.db = db
        self.request = request
        self.request_id = request_id
        self.sender = sender
        self.name = name
        self.depth = depth
        self.nodes = {n["id"]: n for n in graph_data.get("nodes", [])}
        self.memo = {}
        
        # Injected callbacks to prevent circular imports with proxy.py
        self.reverse_proxy_fn = reverse_proxy_fn
        self.resolve_target_fn = resolve_target_fn
        
        self.links = {l[0]: l for l in graph_data.get("links", [])}
        if not self.links:
            for n in self.nodes.values():
                for idx, out in enumerate(n.get("outputs", [])):
                    if out.get("links"):
                        for lid in out["links"]:
                            self.links[lid] = [lid, n["id"], idx, None, None, None]

    async def execute(self, messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
        self.initial_messages = messages
        exit_node = next((n for n in self.nodes.values() if n["type"] == "hub/output"), None)
        if not exit_node:
            logger.warning(f"Workflow '{self.name}' has no Output node.")
            return await self.resolve_target_fn(self.db, "auto", messages, self.depth + 1, self.request, self.request_id, self.sender)

        if "inputs" in exit_node and exit_node["inputs"] and exit_node["inputs"][0].get("link") is not None:
            link = self.links.get(exit_node["inputs"][0]["link"])
            if link:
                final_val = await self.resolve_node_output(link[1], link[2])
                
                # If a cognitive node returns a finalized tuple request
                if isinstance(final_val, tuple) and len(final_val) == 2 and isinstance(final_val[1], list):
                    return final_val
                
                # Otherwise, it's a raw string (Composer, Datastore, etc)
                return "__result__", [{"role": "assistant", "content": str(final_val)}]

        return await self.resolve_target_fn(self.db, "auto", messages, self.depth + 1, self.request, self.request_id, self.sender)

    async def _resolve_input(self, node: Dict[str, Any], idx: int) -> Any:
        if not node.get("inputs") or idx >= len(node["inputs"]): return None
        link_id = node["inputs"][idx].get("link")
        if link_id is None: return None
        link = self.links.get(link_id)
        if not link: return None
        return await self.resolve_node_output(link[1], link[2])

    async def _resolve_input_by_name(self, node: Dict[str, Any], name: str) -> Any:
        if not node.get("inputs"): return None
        for idx, inp in enumerate(node["inputs"]):
            if inp.get("name") == name:
                return await self._resolve_input(node, idx)
        return None

    async def resolve_node_output(self, node_id: int, slot_idx: int = 0) -> Any:
        memo_key = f"{node_id}_{slot_idx}"
        if memo_key in self.memo: return self.memo[memo_key]
        
        node = self.nodes.get(node_id)
        if not node: return None
        result = await self._evaluate_node(node, slot_idx)
        self.memo[memo_key] = result
        return result

    async def execute_cognitive_path(self, link_id_or_name: Any, history: Any) -> str:
        if isinstance(link_id_or_name, int):
            link = self.links.get(link_id_or_name)
            if not link: return ""
            src_node = self.nodes.get(link[1])
            if not src_node: return ""
            
            if src_node["type"] == "hub/model":
                bundle = await self.resolve_node_output(link[1], link[2])
                if isinstance(bundle, dict) and bundle.get("type") == "expert_bundle":
                    m_target = bundle["model"]
                    p_part = f"## Identity\n{bundle['persona']}" if bundle.get('persona') else ""
                    s_part = "\n\n## Expert Skills\n" + "\n\n".join(bundle['skills']) if bundle.get('skills') else ""
                    persona_injection = p_part + s_part
                    inference_options = {"temperature": bundle["temperature"]} if bundle.get("temperature") is not None else {}
                else: return ""
            elif src_node["type"] not in ("hub/llm_chat", "hub/llm_instruct"):
                return str(await self.resolve_node_output(link[1], link[2]))
            else:
                m_target = src_node.get("properties", {}).get("model", "auto")
                persona_injection = ""
                inference_options = {}
        else:
            m_target = str(link_id_or_name)
            persona_injection = ""
            inference_options = {}

        hydrated_history = list(history) if isinstance(history, list) else [{"role": "user", "content": str(history)}]
        if persona_injection:
            hydrated_history = [m for m in hydrated_history if m.get("role") != "system"]
            hydrated_history.insert(0, {"role": "system", "content": persona_injection})

        from app.database.models import Workflow
        wf_check = await self.db.execute(select(Workflow).filter(Workflow.name == m_target, Workflow.is_active == True))
        if wf_check.scalars().first():
            res_model, res_msgs = await self.resolve_target_fn(self.db, m_target, hydrated_history, self.depth+1, self.request, self.request_id, self.sender)
            servers = await server_crud.get_servers_with_model(self.db, res_model)
            if not servers: return f"[Error] Sub-workflow '{res_model}' offline."
            resp, _ = await self.reverse_proxy_fn(self.request, "chat", servers, json.dumps({"model": res_model, "messages": res_msgs, "stream": False}).encode(), is_subrequest=True)
            return json.loads(resp.body.decode()).get("message", {}).get("content", "") if hasattr(resp, 'body') else ""

        servers = await server_crud.get_servers_with_model(self.db, m_target)
        if not servers: return f"[Error] Expert '{m_target}' offline."
        
        payload = {"model": m_target, "messages": hydrated_history, "stream": False}
        if inference_options: payload["options"] = inference_options
        resp, _ = await self.reverse_proxy_fn(self.request, "chat", servers, json.dumps(payload).encode(), is_subrequest=True, sender="graph-moe-expert")
        return json.loads(resp.body.decode()).get("message", {}).get("content", "") if hasattr(resp, 'body') else ""

    async def _evaluate_node(self, node: Dict[str, Any], output_slot_idx: int) -> Any:
        ntype = node["type"]
        props = node.get("properties", {})

        # --- DEBUG MODE TRACING ---
        if self.request.app.state.settings.enable_debug_mode:
            node_title = node.get("title") or ntype.split("/")[-1].upper()
            logger.info(f"DEBUG: Executing Graph Node [{node_title}] (ID: {node['id']})")
            event_manager.emit(ProxyEvent(
                "active", 
                self.request_id, 
                node_title, 
                "Workflow Engine", 
                self.sender, 
                error_message=f"Step: {node_title}..."
            ))
        
        # 1. Plugin Execution (Self-contained nodes)
        node_cls = NodeRegistry.get_node(ntype)
        if node_cls:
            plugin = node_cls()
            return await plugin.execute(self, node, output_slot_idx)

        # 2. Legacy Fallback Execution
        if ntype == "hub/input":
            return self.initial_messages if output_slot_idx == 0 else {}
            
        elif ntype == "hub/moe":
            history = await self._resolve_input_by_name(node, "User Context")
            if history is None: history = await self._resolve_input(node, 0) or []
            
            expert_tasks, expert_names = [], []
            
            for i in range(len(node.get("inputs", []))):
                inp = node["inputs"][i]
                if inp.get("name") == "User Context" or inp.get("name") == "Settings" or "Settings" in str(inp.get("label", "")): continue
                if inp.get("link"):
                    link = self.links.get(inp["link"])
                    src_name = self.nodes[link[1]].get("properties", {}).get("model", f"Expert {i}") if link and link[1] in self.nodes else f"Expert {i}"
                    expert_names.append(src_name)
                    expert_tasks.append(self.execute_cognitive_path(inp["link"], history))
                    
            if not expert_tasks: return "No experts connected."
            if props.get("send_status_update"):
                event_manager.emit(ProxyEvent("active", self.request_id, "MoE Block", "Gateway", self.sender, error_message=f"Engaging: {', '.join(expert_names)}..."))

            responses = await asyncio.gather(*expert_tasks, return_exceptions=True)
            panel_data = ""
            for i, resp in enumerate(responses):
                val = str(resp) if not isinstance(resp, Exception) else f"Error: {str(resp)}"
                panel_data += f"### EXPERT {i+1} FEEDBACK:\n{val}\n\n"

            orchestrator_model = props.get("orchestrator", "auto")
            synth_settings = await self._resolve_input_by_name(node, "Settings")
            if synth_settings is None: synth_settings = await self._resolve_input(node, 3) if len(node.get("inputs", [])) > 3 else {}

            final_messages = list(history) if isinstance(history, list) else [{"role": "user", "content": str(history)}]
            persona_reminder = f"\n\nCRITICAL: Maintain identity:\n{final_messages[0]['content']}" if final_messages and final_messages[0].get("role") == "system" else ""
            final_messages.append({"role": "user", "content": f"### EXPERT PANEL OUTPUTS\n{panel_data}\n\n### SYNTHESIS MANDATE\n{props.get('system_prompt', '')}{persona_reminder}"})
            
            servers = await server_crud.get_servers_with_model(self.db, orchestrator_model)
            if not servers: return f"[Error] Orchestrator '{orchestrator_model}' offline."
            
            payload = {"model": orchestrator_model, "messages": final_messages, "stream": False}
            if isinstance(synth_settings, dict): payload["options"] = synth_settings
            
            resp_obj, _ = await self.reverse_proxy_fn(self.request, "chat", servers, json.dumps(payload).encode(), is_subrequest=True, sender="graph-moe")
            if hasattr(resp_obj, 'body'):
                try: return json.loads(resp_obj.body.decode()).get("message", {}).get("content", "Error: Empty")
                except Exception as e: return f"JSON Error: {str(e)}"
            return "Synthesis Error: No response."

        elif ntype == "hub/system_composer":
            parts = []
            for i in range(len(node.get("inputs", []))):
                val = await self._resolve_input(node, i)
                if val: parts.append(str(val))
            return "\n\n".join(parts)

        elif ntype == "hub/system_merger":
            identity = await self._resolve_input_by_name(node, "Base Identity")
            if identity is None: identity = await self._resolve_input(node, 0) or ""
            
            skills = []
            for idx, inp in enumerate(node.get("inputs", [])):
                if inp.get("name", "").startswith("Skill"):
                    val = await self._resolve_input(node, idx)
                    if val: skills.append(str(val))
            return f"## Identity\n{identity}" + ("\n\n## Capabilities & Skills\n" + "\n\n".join(skills) if skills else "")

        elif ntype == "hub/skill":
            from app.core.skills_manager import SkillsManager
            skill = next((s for s in SkillsManager.get_all_skills() if s["name"] == props.get("name")), None)
            return skill["raw"] if skill else ""
            
        elif ntype == "hub/personality":
            from app.core.personalities_manager import PersonalityManager
            import re
            p = next((x for x in PersonalityManager.get_all_personalities() if x["name"] == props.get("name")), None)
            if not p: return ""
            if output_slot_idx == 0: return re.sub(r'^---\n.*?\n---\n', '', p["raw"], flags=re.DOTALL).strip()
            elif output_slot_idx == 3: return {"temperature": props.get("temperature", 0.7)}
            return ""

        elif ntype == "hub/mcp":
            return {"type": "mcp", "name": props.get("name"), "config": {"type": props.get("type"), "url": props.get("url"), "icon": props.get("icon")}}

        elif ntype == "hub/agent":
            history = await self._resolve_input_by_name(node, "Messages")
            if history is None: history = await self._resolve_input(node, 0) or []
            if not isinstance(history, list): history = [{"role": "user", "content": str(history)}]
            
            tools = []
            for idx, inp in enumerate(node.get("inputs", [])):
                if inp.get("name", "").startswith("Tool"):
                    val = await self._resolve_input(node, idx)
                    if val: tools.append(val)
            
            agent_history = [{"role": "system", "content": props.get("system_prompt", "")}] + history
            target_model = props.get("model", "auto")
            final_answer = ""
            
            for loop_count in range(1, int(props.get("max_loops", 5)) + 1):
                event_manager.emit(ProxyEvent("active", self.request_id, f"Agent Loop {loop_count}", target_model, self.sender, error_message=f"Thinking... (Step {loop_count})"))
                servers = await server_crud.get_servers_with_model(self.db, target_model)
                if not servers: return "[Error] Agent model offline."
                
                resp_obj, _ = await self.reverse_proxy_fn(self.request, "chat", servers, json.dumps({"model": target_model, "messages": agent_history, "stream": False, "tools": [t for t in tools if t.get("type") != "mcp"]}).encode(), is_subrequest=True)
                if not hasattr(resp_obj, 'body'): break
                msg = json.loads(resp_obj.body.decode()).get("message", {})
                
                if msg.get("content"): event_manager.emit(ProxyEvent("active", self.request_id, "Agent Thought", target_model, self.sender, error_message=msg["content"][:100] + "..."))
                if not msg.get("tool_calls"):
                    final_answer = msg.get("content", "")
                    break
                    
                agent_history.append(msg)
                for call in msg.get("tool_calls", []):
                    t_name = call.get("function", {}).get("name")
                    event_manager.emit(ProxyEvent("active", self.request_id, "Tool Execution", "Local", self.sender, error_message=f"Executing: {t_name}..."))
                    agent_history.append({"role": "tool", "name": t_name, "content": f"[Tool {t_name} simulated]"})
            return final_answer or "Agent reached max loops."

        elif ntype == "hub/settings_modifier":
            base = await self._resolve_input(node, 0) or {}
            if not isinstance(base, dict): base = {}
            updated = base.copy()
            updated["temperature"] = props.get("temperature", 0.7)
            return updated

        elif ntype == "hub/extract_text":
            msgs = await self._resolve_input_by_name(node, "Messages")
            if msgs is None: msgs = await self._resolve_input(node, 0)
            return msgs[-1].get("content", "") if msgs and isinstance(msgs, list) else ""

        elif ntype == "hub/create_message":
            text = await self._resolve_input_by_name(node, "Text")
            if text is None: text = await self._resolve_input(node, 0)
            return {"role": props.get("role", "user"), "content": str(text)} if text else None

        elif ntype == "hub/append_message":
            history = await self._resolve_input_by_name(node, "Messages")
            if history is None: history = await self._resolve_input(node, 0) or []
            content = await self._resolve_input_by_name(node, "Content")
            if content is None: content = await self._resolve_input(node, 1)
            
            updated = list(history) if isinstance(history, list) else [history] if history else []
            if content: updated.append({"role": props.get("role", "user"), "content": str(content)})
            return updated

        elif ntype == "hub/system_modifier":
            history = await self._resolve_input_by_name(node, "Messages")
            if history is None: history = await self._resolve_input(node, 0) or []
            sys_prompt = await self._resolve_input_by_name(node, "System Prompt")
            if sys_prompt is None: sys_prompt = await self._resolve_input(node, 1)
            
            if not isinstance(history, list): history = [{"role": "user", "content": str(history)}]
            updated = [m for m in history if m.get("role") != "system"]
            if sys_prompt: updated.insert(0, {"role": "system", "content": str(sys_prompt)})
            return updated

        elif ntype == "hub/model":
            from app.core.personalities_manager import PersonalityManager
            from app.core.skills_manager import SkillsManager
            import re
            
            persona_text = ""
            if props.get("persona"):
                match = next((x for x in PersonalityManager.get_all_personalities() if x["name"] == props.get("persona")), None)
                if match: persona_text = re.sub(r'^---\n.*?\n---\n', '', match["raw"], flags=re.DOTALL).strip()
                
            skills_text_list = []
            if props.get("skills"):
                s_list = SkillsManager.get_all_skills()
                for name in props["skills"]:
                    match = next((x for x in s_list if x["name"] == name), None)
                    if match: skills_text_list.append(re.sub(r'^---\n.*?\n---\n', '', match["raw"], flags=re.DOTALL).strip())
                    
            return {"type": "expert_bundle", "model": props.get("model", "auto"), "persona": persona_text, "skills": skills_text_list, "temperature": None}

        elif ntype == "hub/autorouter":
            history = await self._resolve_input(node, 0) or []
            user_text = (history[-1].get("content", "") if history else "").lower()
            selected_slot = -1
            
            for rule in props.get("rules", []):
                if not rule.get("keywords"): continue
                for kw in [k.strip().lower() for k in rule["keywords"].split(",")]:
                    if kw.startswith("/") and kw.endswith("/"):
                        try:
                            if re.search(kw[1:-1], user_text, re.I): selected_slot = rule["slot"]; break
                        except: pass
                    elif kw in user_text:
                        selected_slot = rule["slot"]; break
                if selected_slot != -1: break

            if selected_slot == -1 and props.get("use_semantic") and props.get("rules"):
                c_model = props.get("classifier_model", "auto")
                intents_map = {r["slot"]: r["intent"] for r in props["rules"] if r["intent"]}
                if intents_map:
                    prompt = f"Classify this: '{user_text}'\nPaths:\n" + "\n".join([f"- PATH {s}: {d}" for s, d in intents_map.items()]) + "\nOutput ONLY 'PATH X'."
                    event_manager.emit(ProxyEvent("active", self.request_id, "Auto Router", c_model, self.sender, error_message="Classifying intent..."))
                    servers = await server_crud.get_servers_with_model(self.db, c_model)
                    if servers:
                        resp, _ = await self.reverse_proxy_fn(self.request, "chat", servers, json.dumps({"model": c_model, "messages": [{"role": "user", "content": prompt}], "stream": False}).encode(), is_subrequest=True)
                        if hasattr(resp, 'body'):
                            match = re.search(r'PATH (\d+)', json.loads(resp.body.decode()).get("message", {}).get("content", "").upper())
                            if match: selected_slot = int(match.group(1))

            if selected_slot == -1 or selected_slot >= len(node.get("inputs", [])): selected_slot = 1
            if node["inputs"][selected_slot].get("link"):
                return await self.execute_cognitive_path(node["inputs"][selected_slot]["link"], history)
            return "Router picked empty slot."

        elif ntype == "hub/tool":
            meta = props.get("metadata", {})
            if "type" in meta: return meta
            if "name" in meta and "description" in meta:
                return {"type": "function", "function": {"name": meta["name"], "description": meta["description"], "parameters": meta.get("parameters", {"type": "object", "properties": {}})}}
            return None

        return None
