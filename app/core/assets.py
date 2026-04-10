import os
import httpx
import logging
import json
from pathlib import Path

logger = logging.getLogger(__name__)

# Map of local filenames to their remote source URLs
REQUIRED_ASSETS = {
    "gl-matrix.js": "https://cdnjs.cloudflare.com/ajax/libs/gl-matrix/2.8.1/gl-matrix-min.js",
    "litegraph.js": "https://unpkg.com/litegraph.js@0.7.10/build/litegraph.js",
    "litegraph.css": "https://unpkg.com/litegraph.js@0.7.10/css/litegraph.css",
    "chart.js": "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js",
    "marked.min.js": "https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js",
    "highlight.min.js": "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js",
    "highlight-dark.min.css": "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css",
}

VENDOR_DIR = Path("app/static/vendor")

async def ensure_local_assets():
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for filename, url in REQUIRED_ASSETS.items():
            target_path = VENDOR_DIR / filename
            if not target_path.exists():
                logger.info(f"Downloading vendor asset: {filename}...")
                resp = await client.get(url)
                resp.raise_for_status()
                target_path.write_bytes(resp.content)