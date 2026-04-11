import json
import logging
from typing import Dict, Any
from app.nodes.base import BaseNode
from app.crud import server_crud

logger = logging.getLogger(__name__)

class VisionNode(BaseNode):
    node_type = "hub/vision"
    node_title = "Vision Expert"
    node_category = "Serving & Cognition"
    
    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeVision() {
    this.addInput("Input", "string");
    this.addOutput("Result", "string");
    this.properties = { vlm: "auto", text: "auto" };
    this.vWidget = this.addWidget("combo", "VLM Expert", this.properties.vlm, (v) => { this.properties.vlm = v; pushHistoryState(); }, { values: available_models });
    this.title = "👁️ VISION EXPERT";
    this.color = "#0369a1";
    this.bgcolor = "#0c4a6e";
    this.size = [280, 80];
    this.serialize_widgets = true;
}
NodeVision.prototype.onConfigure = function() {
    if(this.vWidget) this.vWidget.value = this.properties.vlm;
};
LiteGraph.registerNodeType("hub/vision", NodeVision);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        input_text = await engine._resolve_input(node, 0)
        vlm_model = node["properties"].get("vlm", "auto")
        
        # 1. Extract images from initial request
        images = []
        for m in engine.initial_messages:
            if m.get("images"): images.extend(m["images"])
            
        if not images:
            return f"[Vision Error: No images provided] {input_text}"

        # 2. Call VLM to describe images
        vlm_prompt = f"Analyze these images in the context of: {input_text}. Be descriptive."
        vlm_payload = {
            "model": vlm_model,
            "messages": [{"role": "user", "content": vlm_prompt, "images": images}],
            "stream": False
        }
        
        servers = await server_crud.get_servers_with_model(engine.db, vlm_model)
        if not servers: return "[Vision Error: No VLM server online]"
        
        resp, _ = await engine.reverse_proxy_fn(engine.request, "chat", servers, json.dumps(vlm_payload).encode(), is_subrequest=True)
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            description = data.get("message", {}).get("content", "")
            return f"IMAGE ANALYSIS:\n{description}\n\nUSER QUERY:\n{input_text}"
        
        return input_text