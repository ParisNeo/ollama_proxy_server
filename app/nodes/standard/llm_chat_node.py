import logging
from typing import Dict, Any
from app.nodes.base import BaseNode

logger = logging.getLogger(__name__)

class LLMChatNode(BaseNode):
    node_type = "hub/llm_chat"

    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeLLMChat() {
    this.addInput("Messages", "messages");
    this.addInput("Settings", "object");
    this.addInput("Model Override", "string");
    this.addOutput("Content", "string");
    
    this.properties = { model: "auto" };
    
    this.mWidget = this.addWidget("combo", "Model", this.properties.model, (v) => { 
        this.properties.model = v; 
        pushHistoryState();
    }, { values: available_models });

    this.addWidget("button", "+ Add Tool", null, () => {
        this.addInput("Tool " + (this.inputs.length - 2), "tool,array");
        this.size = this.computeSize();
        this.setDirtyCanvas(true, true);
    });

    this.addWidget("button", "ℹ️ Help", null, () => { showNodeHelp("hub/llm_chat"); });
    
    this.color = "#312e81";
    this.bgcolor = "#1e1b4b";
    this.size = this.computeSize();
}
NodeLLMChat.prototype.onConfigure = function() {
    if(this.mWidget) this.mWidget.value = this.properties.model;
};
LiteGraph.registerNodeType("hub/llm_chat", NodeLLMChat);
        """

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        props = node.get("properties", {})
        target_model = str(props.get("model", "auto")).strip()
        final_temp = 0.7
        
        resolved_messages = engine.initial_messages
        
        val = await engine._resolve_input_by_name(node, "Messages")
        if val is None: val = await engine._resolve_input_by_name(node, "Prompt")
        if val is None: val = await engine._resolve_input(node, 0)
        
        import re

        if val:
            resolved_messages = val if isinstance(val, list) else [{"role": "user", "content": str(val)}]

        # Intercept and convert Datastore Image Tags to Multi-modal Content Lists
        processed_messages = []
        for msg in resolved_messages:
            new_msg = msg.copy()
            if isinstance(new_msg.get("content"), str):
                content_str = new_msg["content"]
                img_tags = re.findall(r'\[IMG_DATA:(data:image/.*?;base64,.*?)\]', content_str)
                
                if img_tags:
                    clean_text = re.sub(r'\[IMG_DATA:.*?\]', '', content_str).strip()
                    content_list = [{"type": "text", "text": clean_text}] if clean_text else []
                    
                    for img_data in img_tags:
                        content_list.append({
                            "type": "image_url",
                            "image_url": {"url": img_data}
                        })
                    
                    new_msg["content"] = content_list
            processed_messages.append(new_msg)
            
        resolved_messages = processed_messages

        settings = await engine._resolve_input_by_name(node, "Settings")
        if settings is None: settings = await engine._resolve_input(node, 1)
        if isinstance(settings, dict) and "temperature" in settings:
            final_temp = float(settings["temperature"])

        model_override = await engine._resolve_input_by_name(node, "Model Override")
        if model_override is None: model_override = await engine._resolve_input(node, 2)
        if model_override: target_model = str(model_override).strip()

        final_tools = []
        if "inputs" in node:
            for inp_idx, inp in enumerate(node["inputs"]):
                if inp.get("name", "").startswith("Tool"):
                    tool_data = await engine._resolve_input(node, inp_idx)
                    if tool_data:
                        if isinstance(tool_data, list): final_tools.extend(tool_data)
                        else: final_tools.append(tool_data)

        if engine.request:
            engine.request.state.graph_temperature = final_temp
            if final_tools: engine.request.state.graph_tools = final_tools

        # Instead of executing the LLM here, we return the proxy execution tuple.
        # This matches the expected format for WorkflowEngine to yield control back to the gateway.
        return await engine.resolve_target_fn(engine.db, target_model, resolved_messages, engine.depth + 1, engine.request, engine.request_id, engine.sender)
