import subprocess
import os
import signal
import logging
import platform
import asyncio
import socket
import httpx
import psutil
from typing import Dict, Optional, List, Tuple
from pathlib import Path
from app.core.binary_manager import LLAMACPP_DIR

logger = logging.getLogger(__name__)

class InstanceSupervisor:
    def __init__(self):
        self.processes: Dict[int, subprocess.Popen] = {}

    def get_instance_state(self, instance) -> Tuple[str, Optional[int]]:
        """
        Determines the true state of an instance.
        Returns: (state, pid)
        States: 'RUNNING' (Managed), 'SYSTEM' (Unmanaged/Service), 'STOPPED', 'CONFLICT' (Port busy by non-ollama)
        """
        # 1. Check if we are currently managing this process
        managed_proc = self.processes.get(instance.id)
        if managed_proc and managed_proc.poll() is None:
            return "RUNNING", managed_proc.pid

        # 2. Check if the port is busy physically
        pid_on_port = self._get_pid_on_port(instance.port)
        if pid_on_port:
            # 3. Verify if it's actually an Ollama instance
            if self._is_ollama_responding(instance.port):
                return "SYSTEM", pid_on_port
            return "CONFLICT", pid_on_port

        return "STOPPED", None

    def _get_pid_on_port(self, port: int) -> Optional[int]:
        """Finds the PID of the process listening on a specific port."""
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                    return conn.pid
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            # Fallback for systems where net_connections requires root
            return None
        return None

    def _is_ollama_responding(self, port: int) -> bool:
        """Probes the port to see if an Ollama API is active."""
        try:
            # Short timeout to prevent UI lag
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    async def start_instance(self, instance):
        state, _ = self.get_instance_state(instance)
        if state in ("RUNNING", "SYSTEM", "CONFLICT"):
            logger.warning(f"Cannot start instance {instance.name}: Port {instance.port} is {state}")
            return False

        env = os.environ.copy()
        if instance.gpu_ids:
            env["CUDA_VISIBLE_DEVICES"] = str(instance.gpu_ids)

        is_win = platform.system() == "Windows"
        cmd = []

        if instance.backend_type == "ollama":
            binary = "ollama.exe" if is_win else "ollama"
            cmd = [binary, "serve"]
            env["OLLAMA_HOST"] = f"127.0.0.1:{instance.port}"
            if instance.model_path:
                env["OLLAMA_MODELS"] = str(Path(instance.model_path).absolute())

        elif instance.backend_type == "llamacpp":
            # Target the internal binary from Binary Hub
            binary = str(LLAMACPP_DIR / ("llama-server.exe" if is_win else "llama-server"))
            if not Path(binary).exists():
                logger.error(f"Llama.cpp binary not found at {binary}. Please check Binary Hub.")
                return False
            
            cmd = [
                binary,
                "--model", str(instance.model_path),
                "--port", str(instance.port),
                "--ctx-size", str(instance.ctx_size or 4096),
                "--threads", str(instance.threads or 8),
                "--n-gpu-layers", str(instance.n_gpu_layers or 99),
                "--host", "127.0.0.1"
            ]

        elif instance.backend_type == "vllm":
            # vLLM usually runs via python module or 'vllm' entrypoint
            cmd = [
                "python", "-m", "vllm.entrypoints.openai.api_server",
                "--model", str(instance.model_path),
                "--port", str(instance.port),
                "--tensor-parallel-size", str(instance.tensor_parallel_size or 1),
                "--host", "127.0.0.1"
            ]

        try:
            logger.info(f"Launching {instance.backend_type} instance '{instance.name}' on port {instance.port}")
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if is_win else 0
            )
            self.processes[instance.id] = proc
            return True
        except Exception as e:
            logger.error(f"Failed to launch {instance.backend_type} binary: {e}")
            return False

    async def stop_instance(self, instance_id: int):
        proc = self.processes.get(instance_id)
        if not proc:
            return False

        if platform.system() == "Windows":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            
        if instance_id in self.processes:
            del self.processes[instance_id]
        return True

    async def discover_local_instances(self, managed_ports: List[int], start_port: int, end_port: int) -> List[dict]:
        discovered = []
        for port in range(start_port, end_port + 1):
            if port in managed_ports: continue
            
            # Use psutil to check process name if possible
            pid = self._get_pid_on_port(port)
            if pid:
                try:
                    p = psutil.Process(pid)
                    p_name = p.name().lower()
                    # Catch Windows 'ollama app.exe' and Linux/Mac 'ollama'
                    if "ollama" in p_name and self._is_ollama_responding(port):
                        discovered.append({"port": port, "pid": pid, "name": p.name()})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # Fallback to pure socket probe
                    if self._is_ollama_responding(port):
                        discovered.append({"port": port, "pid": "Unknown", "name": "System Ollama"})
        return discovered

supervisor = InstanceSupervisor()