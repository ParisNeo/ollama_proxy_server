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
from ascii_colors import trace_exception
logger = logging.getLogger(__name__)

class WorkflowEngine:
    def __init__(self, db, request: Request, request_id: str, sender: str, name: str, graph_data: Dict[str, Any], depth: int, reverse_proxy_fn, resolve_target_fn, call_stack: List[str] = None):
        self.db = db
        self.request = request
        self.request_id = request_id
        self.sender = sender
        self.name = name
        self.depth = depth
        self.call_stack = call_stack or []
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
        """
        Executes the graph. 
        If the terminal node is a 'Generator' (LLM), it returns (model, messages) 
        to allow the Gateway to stream directly to the client.
        """
        self.initial_messages = messages
        
        # UI FIX: Open a single unified block for the entire workflow execution
        cb = getattr(self.request.state, "stream_callback", None)
        if cb and self.depth == 0:
            await cb(f'<processing type="workflow" title="ORCHESTRATING: {self.name.upper()}">\n')

        try:
            exit_node = next((n for n in self.nodes.values() if n["type"] == "hub/output"), None)
            if not exit_node:
                    return await self.resolve_target_fn(self.db, "auto", messages, self.depth + 1, self.request, self.request_id, self.sender)

            if "inputs" in exit_node and exit_node["inputs"] and exit_node["inputs"][0].get("link") is not None:
                link = self.links.get(exit_node["inputs"][0]["link"])
                if link:
                    src_node = self.nodes.get(link[1])
                    # --- LATENCY FIX: Terminal LLM Passthrough ---
                    if src_node and src_node["type"] in ("hub/llm_chat", "hub/llm_instruct", "hub/model"):
                        props = src_node.get("properties", {})
                        target_model = str(props.get("model", "auto")).strip()
                        
                        val = await self._resolve_input_by_name(src_node, "Messages")
                        if val is None: val = await self._resolve_input_by_name(src_node, "Prompt")
                        if val is None: val = await self._resolve_input(src_node, 0)
                        
                        resolved_messages = val if isinstance(val, list) else [{"role": "user", "content": str(val)}]

                        settings = await self._resolve_input_by_name(src_node, "Settings")
                        if settings is None: settings = await self._resolve_input(src_node, 1)
                        if isinstance(settings, dict) and self.request:
                            self.request.state.graph_temperature = settings.get("temperature")

                        final_tools = []
                        for inp_idx, inp in enumerate(src_node.get("inputs", [])):
                            if inp.get("name", "").startswith("Tool"):
                                tool_data = await self._resolve_input(src_node, inp_idx)
                                if tool_data:
                                    if isinstance(tool_data, list): final_tools.extend(tool_data)
                                    else: final_tools.append(tool_data)
                        
                        if final_tools and self.request:
                            self.request.state.graph_tools = final_tools

                        # Recursively resolve with current call stack
                        return await self.resolve_target_fn(
                            self.db, target_model, resolved_messages, self.depth + 1, 
                            self.request, self.request_id, self.sender, call_stack=self.call_stack
                        )

                    # For static outputs (Composers, Datastores), resolve normally
                    final_val = await self.resolve_node_output(link[1], link[2])
                    if isinstance(final_val, tuple): return final_val
                    return "__result__", [{"role": "assistant", "content": str(final_val)}]

                return await self.resolve_target_fn(self.db, "auto", messages, self.depth + 1, self.request, self.request_id, self.sender)
        except Exception as ex:
            trace_exception(ex)
            raise
        finally:
            # UI FIX: Close the block ONLY at the root level after all nodes finish
            if cb and self.depth == 0:
                await cb('</processing>\n')


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
        """
        Executes an isolated AI call for a specific path in the graph.
        Handles both raw model strings and structured Expert Bundles.
        """
        m_target = "auto"
        persona_injection = ""
        tools_to_attach = []
        inference_options = {}

        if isinstance(link_id_or_name, int):
            link = self.links.get(link_id_or_name)
            if not link: return ""
            
            # Resolve the source data (might be a string or a bundle dict)
            source_data = await self.resolve_node_output(link[1], link[2])
            
            if isinstance(source_data, dict) and source_data.get("type") == "expert_bundle":
                # --- HYDRATE EXPERT BUNDLE ---
                m_target = source_data.get("model", "auto")
                
                # Extract components safely
                personality = source_data.get('personality', '')
                skills = source_data.get('skills', [])
                
                # Build strings safely
                p_part = f"## Identity\n{personality}" if personality else ""
                s_part = "\n\n## Expert Capabilities\n" + "\n\n".join(skills) if skills else ""
                
                persona_injection = (p_part + s_part).strip()
                
                tools_to_attach = source_data.get("tools", [])
                if source_data.get("temperature") is not None:
                    inference_options["temperature"] = source_data["temperature"]
            else:
                # Legacy / String path
                m_target = str(source_data or "auto")
        else:
            m_target = str(link_id_or_name)

        # 1. Prepare Conversation History
        hydrated_history = list(history) if isinstance(history, list) else [{"role": "user", "content": str(history)}]
        
        # 2. Inject Persona if provided
        if persona_injection:
            # Strip previous system prompts to ensure the Expert's specific soul takes precedence
            hydrated_history = [m for m in hydrated_history if m.get("role") != "system"]
            hydrated_history.insert(0, {"role": "system", "content": persona_injection})

        # 3. Resolve Workflows (Recursive)
        from app.database.models import Workflow
        wf_check = await self.db.execute(select(Workflow).filter(Workflow.name == m_target, Workflow.is_active == True))
        if wf_check.scalars().first():
            res_model, res_msgs = await self.resolve_target_fn(self.db, m_target, hydrated_history, self.depth+1, self.request, self.request_id, self.sender, call_stack=self.call_stack)
            m_target = res_model
            hydrated_history = res_msgs

        # 4. Execute Backend Call
        servers = await server_crud.get_servers_with_model(self.db, m_target)
        if not servers: return f"[Error] Expert Model '{m_target}' is currently offline."
        
        payload = {
            "model": m_target, 
            "messages": hydrated_history, 
            "stream": False,
            "tools": tools_to_attach,
            "options": inference_options
        }
        
        resp, _ = await self.reverse_proxy_fn(self.request, "chat", servers, json.dumps(payload).encode(), is_subrequest=True, sender="graph-expert-path")
        
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            return data.get("message", {}).get("content", "Empty response.")
        
        return ""

    async def _evaluate_node(self, node: Dict[str, Any], output_slot_idx: int) -> Any:
        ntype = node["type"]
        props = node.get("properties", {})
        
        # --- DEBUG MODE TRACING ---
        enable_debug = False
        if self.request and hasattr(self.request, 'app'):
            enable_debug = self.request.app.state.settings.enable_debug_mode

        # Mapping generic types to meaningful actions
        raw_title = node.get("title") or ntype.split("/")[-1].replace("_", " ")
        display_title = raw_title.upper()

        if enable_debug and ntype not in ("hub/input", "hub/output"):
            # Telemetry update for Live View
            event_manager.emit(ProxyEvent(
                event_type="active", 
                request_id=self.request_id, 
                model=self.name,
                server="Workflow Engine", 
                sender=self.sender,
                error_message=f"Step: {display_title}"
            ))
            
            cb = getattr(self.request.state, "stream_callback", None)
            if cb:
                # UI FIX: Append to the unified block instead of opening a new one
                await cb(f'* Processing: {display_title}...\n')

        # 1. Plugin Execution
        node_cls = NodeRegistry.get_node(ntype)
        if node_cls:
            plugin = node_cls()
            return await plugin.execute(self, node, output_slot_idx)
        return None
