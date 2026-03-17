import subprocess
import os
import signal
import logging
import platform
import asyncio
import socket
import httpx
from typing import Dict, Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)

class InstanceSupervisor:
    def __init__(self):
        self.processes: Dict[int, subprocess.Popen] = {}

    def _get_env(self, instance) -> dict:
        env = os.environ.copy()
        env["OLLAMA_HOST"] = f"127.0.0.1:{instance.port}"
        if instance.gpu_ids:
            env["CUDA_VISIBLE_DEVICES"] = instance.gpu_ids
        if instance.models_path:
            # Ensure path is absolute for Ollama
            env["OLLAMA_MODELS"] = str(Path(instance.models_path).absolute())
        env["OLLAMA_KEEP_ALIVE"] = instance.keep_alive
        env["OLLAMA_ORIGINS"] = "*"
        return env

    async def start_instance(self, instance):
        if instance.id in self.processes:
            await self.stop_instance(instance.id)

        binary = "ollama.exe" if platform.system() == "Windows" else "ollama"
        
        try:
            logger.info(f"Starting managed instance {instance.name} on port {instance.port}")
            proc = subprocess.Popen(
                [binary, "serve"],
                env=self._get_env(instance),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if platform.system() == "Windows" else 0
            )
            self.processes[instance.id] = proc
            return True
        except Exception as e:
            logger.error(f"Failed to start instance {instance.name}: {e}")
            return False

    async def stop_instance(self, instance_id: int):
        proc = self.processes.get(instance_id)
        if not proc:
            return

        logger.info(f"Stopping managed instance ID {instance_id}")
        if platform.system() == "Windows":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        
        # Give it a moment to shut down
        for _ in range(10):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.5)
        
        if proc.poll() is None:
            proc.kill()
            
        del self.processes[instance_id]

    def is_running(self, instance_id: int) -> bool:
        proc = self.processes.get(instance_id)
        return proc is not None and proc.poll() is None

    async def discover_local_instances(self, existing_ports: List[int], start_port: int = 11434, end_port: int = 11445) -> List[dict]:
        """Scans local ports to find running Ollama instances not managed by us."""
        discovered = []
        # Use provided range
        for port in range(start_port, end_port + 1):
            if port in existing_ports:
                continue
            
            if self._is_port_open(port):
                # Verify it's actually Ollama
                try:
                    async with httpx.AsyncClient(timeout=0.5) as client:
                        resp = await client.get(f"http://127.0.0.1:{port}/api/tags")
                        if resp.status_code == 200:
                            discovered.append({"port": port, "type": "Ollama"})
                except Exception:
                    continue
        return discovered

    def _is_port_open(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            return s.connect_ex(('127.0.0.1', port)) == 0

supervisor = InstanceSupervisor()