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

        # 2. Legacy Fallback Execution (Only for nodes not yet ported to plugins)
        return None
