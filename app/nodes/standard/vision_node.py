import json
import copy
import logging
from typing import Dict, Any, List
from app.nodes.base import BaseNode
from app.crud import server_crud

logger = logging.getLogger(__name__)

class VisionNode(BaseNode):
    node_type = "hub/vision"
    node_title = "Vision Hydrator"
    node_category = "Serving & Cognition"
    node_icon = "👁️"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeVision() {
    this.addInput("In Messages", "messages");
    this.addOutput("Out Messages", "messages");
    this.properties = { vlm: "auto", vlm_prompt_template: "Describe this image in detail. Be precise about elements related to: '{QUERY}'" };
    this.vWidget = this.addWidget("combo", "VLM Model", this.properties.vlm, (v) => { this.properties.vlm = v; pushHistoryState(); }, { values: window.available_models || ["auto"] });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "👁️ VISION HYDRATOR";
    this.color = "#0369a1";
    this.bgcolor = "#0c4a6e";
    this.size = this.computeSize();
    this.serialize_widgets = true;
}
NodeVision.prototype.onConfigure = function() {
    if(this.vWidget) this.vWidget.value = this.properties.vlm;
};
LiteGraph.registerNodeType("hub/vision", NodeVision);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        import copy
        import json
        from app.crud import server_crud
        
        # 1. Get incoming messages list
        # IMPORTANT: Use the link input, not engine.initial_messages, to support pipeline chains
        msgs = await engine._resolve_input(node, 0)
        if msgs is None: msgs = engine.initial_messages
        
        # Deep copy to safely modify history
        hydrated_history = copy.deepcopy(msgs)
        vlm_model = node["properties"].get("vlm", "auto")
        vlm_instruction = node["properties"].get("vlm_prompt_template", "")

        # 2. Iterate through messages and hydrate images
        for msg in hydrated_history:
            # Check content for images
            content = msg.get("content")
            images_to_describe = []
            
            if isinstance(content, list):
                # Identify image parts
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        # Strip data prefix
                        img_b64 = part["image_url"]["url"].split(",")[-1]
                        images_to_describe.append(img_b64)
            
            # Also check side-car images
            if msg.get("images"):
                images_to_describe.extend([img.split(",")[-1] for img in msg["images"] if isinstance(img, str)])

            if not images_to_describe:
                continue

            # Handle Ollama-style side-car images
            if msg.get("images"):
                descs = []
                for img_b64 in msg["images"]:
                    vlm_payload = {"model": vlm_model, "messages": [{"role": "user", "content": vlm_instruction, "images": [img_b64]}], "stream": False, "options": {"temperature": 0.2}}
                    servers = await server_crud.get_servers_with_model(engine.db, vlm_model)
                    if servers:
                        v_res, _ = await engine.reverse_proxy_fn(engine.request, "chat", servers, json.dumps(vlm_payload).encode(), is_subrequest=True, sender="vision-hydrator")
                        if hasattr(v_res, 'body'):
                            descs.append(json.loads(v_res.body.decode()).get("message", {}).get("content", "[Failed]"))
                
                # Append description to content, clear side-car images
                old_text = msg.get("content") if isinstance(msg.get("content"), str) else ""
                msg["content"] = f"{old_text}\n\n[IMAGE ANALYSIS: " + " | ".join(descs) + "]"
                del msg["images"]

        # Return the modified list of messages
        return hydrated_history