from typing import Dict, Any
from app.nodes.base import BaseNode
from app.core.mcp_manager import MCPClient
import asyncio

class MCPConnectorNode(BaseNode):
    node_type = "hub/mcp"
    node_title = "MCP Connector"
    node_category = "Selectors"
    node_icon = "🔌"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        props = node.get("properties", {})
        config = {
            "type": props.get("transport_type", "sse"),
            "url": props.get("url", ""),
            "cmd": props.get("command", ""),
            "headers": props.get("headers", {})
        }
        
        client = MCPClient(props.get("name", "remote-mcp"), config)
        
        # We wrap the MCP client in a format the AgentReasonerNode understands
        return {
            "type": "mcp_bundle",
            "client": client,
            "tools": await client.get_tools()
        }