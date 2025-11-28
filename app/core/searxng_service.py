"""
SearXNG service that runs directly in the proxy server.
Starts SearXNG as a subprocess when the app starts.
"""
import asyncio
import logging
import subprocess
import sys
import time
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Any
import os

logger = logging.getLogger(__name__)

class SearXNGService:
    """Manages SearXNG as an embedded service or connects to existing instance"""
    
    def __init__(self, host: str = "127.0.0.1", port: int = 8888, auto_start: bool = True):
        self.host = host
        self.port = port
        self.process: Optional[subprocess.Popen] = None
        self.url = f"http://{host}:{port}"
        self.searxng_dir = Path("searxng_data")
        self.client = httpx.AsyncClient(timeout=30.0, verify=False)
        self.auto_start = auto_start  # If False, only connect to existing instance
        
    async def is_available(self) -> bool:
        """Check if SearXNG is already running"""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self.url}/")
                return response.status_code == 200
        except:
            return False
    
    async def start(self) -> bool:
        """Start SearXNG service - check if running, if not start standalone script"""
        # First check if SearXNG is already running
        if await self.is_available():
            logger.info(f"SearXNG is already running on {self.url}")
            return True
        
        # If auto_start is False, don't try to start it
        if not self.auto_start:
            logger.warning(f"SearXNG not running on {self.url} and auto_start=False")
            logger.info("Start SearXNG manually with: python searxng_standalone.py")
            return False
        
        # Start SearXNG using the venv script (preferred) or standalone script
        logger.info(f"SearXNG not running on {self.url}, starting service...")
        try:
            project_root = Path(__file__).parent.parent.parent
            
            # Try venv approach first (more reliable)
            venv_python = project_root / "searxng_venv" / "Scripts" / "python.exe"
            venv_script = project_root / "run_searxng_venv.py"
            
            if venv_python.exists() and venv_script.exists():
                logger.info("Using SearXNG virtual environment")
                python_exe = str(venv_python.resolve())
                script = str(venv_script.resolve())
            else:
                # Fallback to standalone script
                logger.info("Using standalone SearXNG script")
                standalone_script = project_root / "searxng_standalone.py"
                if not standalone_script.exists():
                    logger.error(f"Neither venv nor standalone script found")
                    return False
                python_exe = sys.executable
                script = str(standalone_script.resolve())
            
            import subprocess as sp
            creation_flags = 0
            if sys.platform == 'win32':
                creation_flags = sp.CREATE_NO_WINDOW | sp.DETACHED_PROCESS
            
            # Start the script as a background process
            self.process = subprocess.Popen(
                [python_exe, script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
                cwd=str(project_root),
                env=os.environ.copy()
            )
            
            logger.info(f"Started SearXNG standalone process (PID: {self.process.pid})")
            
            # Wait for it to be ready (up to 90 seconds - SearXNG can take time to load engines)
            for i in range(90):
                # Check if process died
                if self.process.poll() is not None:
                    # Try to read error output
                    try:
                        if self.process.stdout:
                            output = self.process.stdout.read(1000).decode('utf-8', errors='ignore')
                            logger.error(f"SearXNG process exited. Output: {output[:500]}")
                    except:
                        pass
                    logger.error(f"SearXNG process exited with code {self.process.returncode}")
                    return False
                
                # Check if it's responding
                if await self.is_available():
                    logger.info(f"SearXNG started successfully on {self.url}")
                    return True
                
                if i % 10 == 0 and i > 0:
                    logger.debug(f"Still waiting for SearXNG to start... ({i}/90)")
                
                await asyncio.sleep(1)
            
            logger.error("SearXNG did not start in time (90 seconds)")
            return False
            
        except Exception as e:
            logger.error(f"Failed to start SearXNG: {e}", exc_info=True)
            return False
    
    async def start_embedded(self) -> bool:
        """Start SearXNG as embedded Python process (old method)"""
        try:
            # Check if SearXNG is installed
            try:
                import searx
                searx_module_path = Path(searx.__file__).parent
                searx_path = searx_module_path / "webapp.py"
                logger.info(f"SearXNG module found at: {searx_module_path}")
                logger.info(f"SearXNG webapp.py at: {searx_path}")
            except ImportError:
                logger.error("SearXNG Python package not installed. Run: pip install -e searxng_src")
                return False
            
            if not searx_path.exists():
                logger.error(f"SearXNG webapp.py not found at {searx_path}")
                return False
            
            # Create data directory
            self.searxng_dir.mkdir(exist_ok=True)
            
            # Create settings file if it doesn't exist
            settings_path = self.searxng_dir / "settings.yml"
            if not settings_path.exists():
                # Use our maximized settings file
                our_settings = Path("searxng_data") / "settings.yml"
                if our_settings.exists():
                    import shutil
                    shutil.copy(our_settings, settings_path)
                    logger.info(f"Using maximized settings from {our_settings}")
                else:
                    # Copy default settings as fallback
                    default_settings = Path("searxng_src") / "searxng_src" / "utils" / "templates" / "etc" / "searxng" / "settings.yml"
                    if default_settings.exists():
                        import shutil
                        shutil.copy(default_settings, settings_path)
                        logger.info(f"Created settings file from defaults at {settings_path}")
                    else:
                        # Create minimal settings
                        settings_path.write_text("""server:
  secret_key: "0bd93f05b88109e3fcf8ec7506432938216b7d70db2d66f8caeff7b988810837"
  base_url: "http://127.0.0.1:8888"
use_default_settings: true
""")
                        logger.warning(f"Created minimal settings file at {settings_path}")
            
            # Ensure settings file exists before setting env var
            if not settings_path.exists():
                logger.error(f"Settings file does not exist at {settings_path}")
                return False
            
            # Set environment variables for SearXNG - use absolute path
            env = os.environ.copy()
            env['SEARXNG_SETTINGS_PATH'] = str(settings_path.resolve())
            env['SEARXNG_BIND_ADDRESS'] = f"{self.host}:{self.port}"
            logger.info(f"SearXNG settings path: {env['SEARXNG_SETTINGS_PATH']}")
            
            # Start SearXNG as subprocess
            logger.info(f"Starting SearXNG on {self.url}...")
            # Use searxng_src as working directory
            work_dir = Path("searxng_src") / "searxng_src"
            if not work_dir.exists():
                work_dir = Path("searxng_src")
            
            logger.info(f"Starting SearXNG with: python {searx_path}")
            logger.info(f"Working directory: {work_dir}")
            logger.info(f"Settings: {env['SEARXNG_SETTINGS_PATH']}")
            
            # Suppress git version check errors and other non-fatal issues
            env['SEARXNG_DISABLE_GIT_VERSION'] = '1'
            env['PYTHONUNBUFFERED'] = '1'
            
            import subprocess as sp
            creation_flags = 0
            if sys.platform == 'win32':
                creation_flags = sp.CREATE_NO_WINDOW
            
            # Run SearXNG using waitress (Windows-compatible WSGI server)
            # This avoids Flask's dev server getaddrinfo issues on Windows
            waitress_script = f"""
import sys
sys.path.insert(0, r'{work_dir.resolve()}')
import os
os.environ['SEARXNG_SETTINGS_PATH'] = r'{settings_path.resolve()}'
from waitress import serve
from searx.webapp import app
serve(app, host='{self.host}', port={self.port}, threads=4)
"""
            
            # Write temporary script
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(waitress_script)
                temp_script = f.name
            
            self.process = subprocess.Popen(
                [sys.executable, temp_script],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(work_dir),
                creationflags=creation_flags,
                text=True,
                bufsize=1
            )
            
            # Log initial output for debugging
            import threading
            def log_output(pipe, prefix):
                try:
                    for line in iter(pipe.readline, b''):
                        if line:
                            logger.debug(f"{prefix}: {line.decode('utf-8', errors='ignore').strip()}")
                except:
                    pass
            
            stdout_thread = threading.Thread(target=log_output, args=(self.process.stdout, "STDOUT"), daemon=True)
            stderr_thread = threading.Thread(target=log_output, args=(self.process.stderr, "STDERR"), daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            
            # Wait for it to be ready (increase timeout to 90 seconds for slow startup)
            for i in range(90):
                # Check if process died
                if self.process.poll() is not None:
                    # Process exited, get error output
                    try:
                        # Read remaining output
                        remaining_output = []
                        if hasattr(self.process, 'stdout') and self.process.stdout:
                            try:
                                import select
                                if select.select([self.process.stdout], [], [], 0)[0]:
                                    line = self.process.stdout.readline()
                                    if line:
                                        remaining_output.append(line.strip())
                            except:
                                pass
                        
                        logger.error(f"SearXNG process exited with code {self.process.returncode}")
                        if remaining_output:
                            logger.error(f"SearXNG final output: {remaining_output[-5:]}")
                    except:
                        pass
                    return False
                
                # Check if port is open
                try:
                    import socket
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.5)
                    result = sock.connect_ex((self.host, self.port))
                    sock.close()
                    
                    if result == 0:
                        # Port is open, verify with HTTP
                        try:
                            async with httpx.AsyncClient(timeout=3.0) as client:
                                response = await client.get(f"{self.url}/")
                                if response.status_code == 200:
                                    logger.info(f"SearXNG started successfully on {self.url}")
                                    return True
                        except:
                            pass  # Port open but HTTP not ready yet
                except:
                    pass
                
                await asyncio.sleep(1)
            
            # Final check
            if self.process.poll() is None:
                # Process still running, check port one more time
                try:
                    import socket
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    result = sock.connect_ex((self.host, self.port))
                    sock.close()
                    if result == 0:
                        logger.info(f"SearXNG appears to be running on {self.url} (port check passed)")
                        return True
                except:
                    pass
            
            logger.error("SearXNG did not start in time or failed to bind to port")
            return False
            
        except Exception as e:
            logger.error(f"Failed to start SearXNG: {e}", exc_info=True)
            return False
    
    async def stop(self):
        """Stop SearXNG service"""
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                logger.info("SearXNG stopped")
            except Exception as e:
                logger.error(f"Error stopping SearXNG: {e}")
            finally:
                self.process = None
    
    def is_running(self) -> bool:
        """Check if SearXNG is running"""
        if not self.process:
            return False
        return self.process.poll() is None
    
    async def search(self, query: str, engines: List[str] = None, format: str = "json") -> Dict[str, Any]:
        """Perform a search using SearXNG"""
        try:
            params = {
                "q": query,
                "format": format
            }
            if engines:
                params["engines"] = ",".join(engines)
            
            response = await self.client.post(
                f"{self.url}/search",
                params=params
            )
            
            if response.status_code == 200:
                if format == "json":
                    return response.json()
                else:
                    return {"html": response.text}
            else:
                logger.error(f"SearXNG search failed: {response.status_code}")
                return {"error": f"Search failed: {response.status_code}"}
                
        except Exception as e:
            logger.error(f"SearXNG search error: {e}", exc_info=True)
            return {"error": str(e)}
