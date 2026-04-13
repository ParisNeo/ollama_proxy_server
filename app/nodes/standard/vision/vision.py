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

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        """
        Processes images in the message history using a VLM and replaces them with text descriptions.
        This allows text-only models to understand image content.
        """
        # Resolve input messages or fallback to initial request history
        msgs = await engine._resolve_input(node, 0) or engine.initial_messages
        if not msgs:
            return []
            
        vlm_model = node["properties"].get("vlm", "auto")
        updated_messages = copy.deepcopy(msgs)
        
        # 1. Gather all images and identify the last user prompt for context
        all_images = []
        last_user_prompt = ""
        
        for msg in updated_messages:
            # Handle Ollama-style standalone 'images' list
            if "images" in msg and msg["images"]:
                all_images.extend(msg["images"])
                del msg["images"] # Remove binary data from this message
            
            content = msg.get("content")
            
            # Handle OpenAI-style multimodal 'content' array
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                        
                    if part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        # Extract raw base64 data
                        img_val = url.split(",")[-1] if "," in url else url
                        if img_val:
                            all_images.append(img_val)
                    elif part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                
                # Flatten multimodal content to a simple string for the downstream text model
                msg["content"] = "\n".join(text_parts).strip()
            
            # Capture last user prompt to guide the VLM's attention
            if msg.get("role") == "user":
                last_user_prompt = msg.get("content", "")

        # 2. If no images were found, return the (potentially flattened) text history
        if not all_images:
            return updated_messages

        # 3. Call the VLM to generate descriptions
        vision_prompt = (
            "Analyze the provided images and describe their contents in detail. "
            f"Pay special attention to elements relevant to this user query:\n\"{last_user_prompt}\"\n\n"
            "Be thorough but concise."
        )
        
        # Build internal VLM payload
        vlm_payload = {
            "model": vlm_model,
            "messages": [
                {
                    "role": "user",
                    "content": vision_prompt,
                    "images": all_images[:10] # Limit to 10 images for performance
                }
            ],
            "stream": False,
            "options": {"temperature": 0.2}
        }

        try:
            # Resolve the VLM target (it might be a router or virtual agent)
            real_vlm, vlm_msgs = await engine.resolve_target_fn(
                engine.db, vlm_model, vlm_payload["messages"], 
                engine.depth + 1, engine.request, engine.request_id, engine.sender
            )
            
            servers = await server_crud.get_servers_with_model(engine.db, real_vlm)
            if not servers:
                logger.warning(f"Vision Hydrator: VLM model '{real_vlm}' is offline. Bypassing analysis.")
                return updated_messages

            # Execute the VLM request through the proxy engine
            resp, _ = await engine.reverse_proxy_fn(
                engine.request, "chat", servers, 
                json.dumps({"model": real_vlm, "messages": vlm_msgs, "stream": False}).encode(), 
                is_subrequest=True,
                sender="vision-hydrator"
            )
            
            if hasattr(resp, 'body'):
                data = json.loads(resp.body.decode())
                description = data.get("message", {}).get("content", "").strip()
                
                if description:
                    # 4. Prepend the description to the last USER message
                    # This provides the text model with the visual context it lacks
                    for i in range(len(updated_messages) - 1, -1, -1):
                        if updated_messages[i].get("role") == "user":
                            orig_text = updated_messages[i].get("content", "")
                            updated_messages[i]["content"] = (
                                f"### CONTEXTUAL IMAGE ANALYSIS:\n{description}\n\n"
                                f"### USER QUERY:\n{orig_text}"
                            ).strip()
                            break
                            
        except Exception as e:
            logger.error(f"Vision Hydrator failure: {e}", exc_info=True)
            # On failure, we still return the cleaned (text-only) messages to prevent 
            # binary image data from crashing the final text model.

        return updated_messages