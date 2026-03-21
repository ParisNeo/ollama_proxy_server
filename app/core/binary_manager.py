import os
import platform
import httpx
import shutil
import zipfile
import tarfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Base directory for internal binaries
BIN_DIR = Path(".lollms/bin")
LLAMACPP_DIR = BIN_DIR / "llamacpp"

class BinaryManager:
    @staticmethod
    def ensure_dirs():
        LLAMACPP_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    async def get_latest_llamacpp_release():
        """Fetches the latest release metadata from GitHub."""
        url = "https://api.github.com/repos/ggerganov/llama.cpp/releases/latest"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def get_os_asset_name():
        """Determines the correct zip/tar name for the current system."""
        sys_name = platform.system().lower()
        arch = platform.machine().lower()
        
        if sys_name == "windows":
            # Target the avx2 or cuda version for gamer PCs
            return "llama-b{}-bin-win-cuda-cu12.4-x64.zip" 
        elif sys_name == "linux":
            return "llama-b{}-bin-ubuntu-x64.tar.gz"
        elif sys_name == "darwin":
            return "llama-b{}-bin-macos-arm64.tar.gz"
        return None

    @staticmethod
    async def download_engine(engine_type="llamacpp"):
        """Downloads and extracts the engine binary."""
        BinaryManager.ensure_dirs()
        
        if engine_type == "llamacpp":
            release = await BinaryManager.get_latest_llamacpp_release()
            version = release['tag_name'].replace('b', '')
            asset_template = BinaryManager.get_os_asset_name()
            
            if not asset_template:
                raise RuntimeError(f"Unsupported OS: {platform.system()}")
            
            target_asset = asset_template.format(version)
            download_url = None
            
            for asset in release['assets']:
                if asset['name'] == target_asset:
                    download_url = asset['browser_download_url']
                    break
            
            if not download_url:
                # Fallback to a simpler asset if the specific CUDA one isn't found
                download_url = release['assets'][0]['browser_download_url']

            logger.info(f"Downloading {engine_type} from {download_url}...")
            
            archive_path = BIN_DIR / target_asset
            async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
                async with client.stream("GET", download_url) as response:
                    with open(archive_path, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)

            # Extraction
            if target_asset.endswith(".zip"):
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(LLAMACPP_DIR)
            else:
                with tarfile.open(archive_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(LLAMACPP_DIR)
            
            os.remove(archive_path)
            
            # Make binary executable on Linux/Mac
            if platform.system() != "Windows":
                for bin_file in LLAMACPP_DIR.glob("llama-*"):
                    bin_file.chmod(0o755)
            
            return version

binary_manager = BinaryManager()