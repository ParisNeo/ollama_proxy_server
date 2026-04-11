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
        from app.core.events import event_manager, ProxyEvent

        # 1. Resolve inputs
        msgs = await engine._resolve_input(node, 0)
        if msgs is None: msgs = engine.initial_messages
        
        hydrated_history = copy.deepcopy(msgs)
        vlm_model = node["properties"].get("vlm", "auto")
        vlm_instruction_template = node["properties"].get("vlm_prompt_template", "Describe this image in detail. Pay special attention to elements relevant to this user query: '{QUERY}'")

        # 2. Iterate through messages and surgically hydrate images
        for msg in hydrated_history:
            content = msg.get("content")
            images_to_describe = []
            original_text = ""
            
            # --- Extract text and images from multimodal list ---
            if isinstance(content, list):
                new_list = []
                for part in content:
                    if not isinstance(part, dict): continue
                    if part.get("type") == "text":
                        original_text += part.get("text", "") + " "
                    elif part.get("type") == "image_url":
                        img_val = part["image_url"]["url"].split(",")[-1]
                        images_to_describe.append(img_val)
            else:
                original_text = str(content or "")

            # --- Extract from side-car images ---
            if msg.get("images"):
                images_to_describe.extend([img.split(",")[-1] for img in msg["images"] if isinstance(img, str)])

            if not images_to_describe:
                continue

            # 3. Analyze Images via VLM
            event_manager.emit(ProxyEvent("active", engine.request_id, "Vision Node", vlm_model, engine.sender, error_message=f"Analyzing {len(images_to_describe)} images..."))
            
            # CONTEXTUAL INJECTION: Replace {QUERY} with the actual text the user sent with the image
            vlm_instruction = vlm_instruction_template.replace("{QUERY}", original_text.strip() or "Describe the image contents.")

            descs = []
            servers = await server_crud.get_servers_with_model(engine.db, vlm_model)
            if not servers:
                descs = ["[Error: No VLM server available for analysis]"]
            else:
                for idx, img_b64 in enumerate(images_to_describe):
                    vlm_payload = {
                        "model": vlm_model, 
                        "messages": [{"role": "user", "content": vlm_instruction, "images": [img_b64]}], 
                        "stream": False
                    }
                    try:
                        v_res, _ = await engine.reverse_proxy_fn(
                            engine.request, "chat", servers, 
                            json.dumps(vlm_payload).encode(), 
                            is_subrequest=True, 
                            sender="vision-hydrator"
                        )
                        if hasattr(v_res, 'body'):
                            data = json.loads(v_res.body.decode())
                            descs.append(data.get("message", {}).get("content", "[Analysis Failed]"))
                    except Exception as e:
                        descs.append(f"[Analysis Error: {str(e)}]")
            
            # 4. SURGICAL TRANSFORMATION: Convert to PURE text
            # This ensures the downstream text-only model never sees image bits
            analysis_block = "\n\n### CONTEXTUAL IMAGE ANALYSIS:\n" + "\n".join(descs)
            msg["content"] = (original_text.strip() + analysis_block).strip()
            
            # Remove image markers to satisfy text-only backends
            if "images" in msg: del msg["images"]

        return hydrated_history