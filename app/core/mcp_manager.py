import asyncio
import json
import os
import subprocess
import logging
import httpx
from typing import Dict, Any, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

class MCPClient:
    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config # {type: 'stdio'|'sse', cmd: '...', url: '...', headers: {}}
        self.process = None
        self._tool_cache = []

    async def get_tools(self) -> List[dict]:
        """Fetches tool schemas from the MCP server."""
        if self.config['type'] == 'sse':
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.config['url']}/tools", headers=self.config.get('headers'))
                return resp.json().get('tools', [])
        else:
            # Stdio one-shot discovery (simplified)
            return await self._call_stdio("list_tools", {})

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        if self.config['type'] == 'sse':
            async with httpx.AsyncClient() as client:
                url = f"{self.config['url']}/tools/call"
                payload = {"name": tool_name, "arguments": arguments}
                resp = await client.post(url, json=payload, headers=self.config.get('headers'), timeout=30.0)
                return resp.json().get('content', [{}])[0].get('text', '')
        else:
            return await self._call_stdio("call_tool", {"name": tool_name, "arguments": arguments})

    async def _call_stdio(self, method: str, params: dict) -> Any:
        """Executes a command via stdio pipes (JSON-RPC)."""
        cmd = self.config['cmd']
        # Use shell execution for npx/python commands
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        rpc_req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        proc.stdin.write((json.dumps(rpc_req) + "\n").encode())
        await proc.stdin.drain()
        
        line = await proc.stdout.readline()
        proc.terminate()
        
        if line:
            res = json.loads(line.decode())
            return res.get('result')
        return None

mcp_manager = {} # Global cache for persistent clients