import subprocess
import os
import signal
import logging
import platform
import asyncio
import socket
import httpx
import psutil
import shutil
import pipmaster as pm
from typing import Dict, Optional, List, Tuple
from pathlib import Path
from app.core.binary_manager import LLAMACPP_DIR

logger = logging.getLogger(__name__)

class InstanceSupervisor:
    def __init__(self):
        self.processes: Dict[int, subprocess.Popen] = {}

    async def get_instance_state(self, instance) -> Tuple[str, Optional[int]]:
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
            if await self._is_ollama_responding(instance.port):
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

    async def _is_ollama_responding(self, port: int) -> bool:
        """Probes the port to see if an Ollama API is active without blocking the loop."""
        def probe():
            try:
                # Short timeout to prevent UI lag
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    return True
            except (socket.timeout, ConnectionRefusedError, OSError):
                return False
        
        from fastapi.concurrency import run_in_threadpool
        return await run_in_threadpool(probe)

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

    def is_ollama_installed(self) -> bool:
        """Checks if the ollama binary is reachable in the system PATH."""
        binary = "ollama.exe" if platform.system() == "Windows" else "ollama"
        return shutil.which(binary) is not None

    def is_vllm_installed(self) -> bool:
        """Checks if vLLM is installed in the current Python environment."""
        return pm.is_installed("vllm")

    async def install_vllm(self, upgrade=False) -> Tuple[bool, str]:
        """Attempts to install or update vLLM using pipmaster."""
        from app.core.events import event_manager, ProxyEvent
        task_name = "Updating" if upgrade else "Installing"
        req_id = "sys_vllm"
        
        try:
            event_manager.emit(ProxyEvent("received", req_id, "vLLM", "Local", "Admin", error_message=f"{task_name} vLLM via pipmaster..."))
            
            # vLLM is heavy, we use pipmaster to handle the async installation
            success = await pm.async_install("vllm", upgrade=upgrade)
            
            if success:
                msg = f"vLLM {'updated' if upgrade else 'installed'} successfully."
                event_manager.emit(ProxyEvent("completed", req_id, "vLLM", "Local", "Admin", error_message=msg))
                return True, msg
            
            err = "pipmaster failed to install vllm."
            event_manager.emit(ProxyEvent("error", req_id, "vLLM", "Local", "Admin", error_message=err))
            return False, err
        except Exception as e:
            event_manager.emit(ProxyEvent("error", req_id, "vLLM", "Local", "Admin", error_message=str(e)))
            return False, str(e)

    async def install_ollama(self) -> Tuple[bool, str]:
        """Attempts to install/update Ollama based on the operating system."""
        from app.core.events import event_manager, ProxyEvent
        sys_name = platform.system()
        req_id = "sys_ollama"
        
        try:
            if sys_name == "Linux" or sys_name == "Darwin":
                event_manager.emit(ProxyEvent("received", req_id, "Ollama", "Local", "Admin", error_message="Downloading Ollama installation script..."))
                cmd = "curl -fsSL https://ollama.com/install.sh | sh"
                
                process = await asyncio.create_subprocess_shell(
                    cmd, 
                    stdout=asyncio.subprocess.PIPE, 
                    stderr=asyncio.subprocess.STDOUT
                )
                
                # Stream logs to UI
                while True:
                    line = await process.stdout.readline()
                    if not line: break
                    event_manager.emit(ProxyEvent("active", req_id, "Ollama", "Local", "Admin", error_message=line.decode().strip()))
                
                await process.wait()
                
                if process.returncode == 0:
                    msg = "Ollama processed successfully via shell script."
                    event_manager.emit(ProxyEvent("completed", req_id, "Ollama", "Local", "Admin", error_message=msg))
                    return True, msg
                return False, "Installation script failed."
            
            elif sys_name == "Windows":
                event_manager.emit(ProxyEvent("received", req_id, "Ollama", "Local", "Admin", error_message="Downloading OllamaSetup.exe to Temp..."))
                installer_url = "https://ollama.com/download/OllamaSetup.exe"
                temp_dir = Path(os.environ.get("TEMP", "."))
                target_path = temp_dir / f"OllamaSetup_{secrets.token_hex(4)}.exe"
                
                async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
                    async with client.stream("GET", installer_url) as resp:
                        resp.raise_for_status()
                        with open(target_path, "wb") as f:
                            async for chunk in resp.aiter_bytes():
                                f.write(chunk)
                
                event_manager.emit(ProxyEvent("active", req_id, "Ollama", "Local", "Admin", error_message="Download finished. Launching UI..."))
                # Use DETACHED_PROCESS flag on Windows to allow installer to outlive the hub
                subprocess.Popen([str(target_path)], shell=True, creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS)
                
                msg = "Windows Installer UI visible. Please follow the instructions on your desktop."
                event_manager.emit(ProxyEvent("completed", req_id, "Ollama", "Local", "Admin", error_message=msg))
                return True, msg
            
            return False, f"Automatic installation not supported on {sys_name}."
        except Exception as e:
            event_manager.emit(ProxyEvent("error", req_id, "Ollama", "Local", "Admin", error_message=str(e)))
            return False, str(e)

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