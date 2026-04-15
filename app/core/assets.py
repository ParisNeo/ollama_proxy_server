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
    "pixi.min.js": "https://cdn.jsdelivr.net/npm/pixi.js@8.18.1/dist/pixi.min.js",
    "tailwindcss.js": "https://cdn.tailwindcss.com",
    "js-yaml.min.js": "https://cdn.jsdelivr.net/npm/js-yaml@4.1.0/dist/js-yaml.min.js",
    "mermaid.min.js": "https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js",
    "codemirror.min.js": "https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.12/codemirror.min.js",
    "codemirror.min.css": "https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.12/codemirror.min.css",
    "codemirror-monokai.min.css": "https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.12/theme/monokai.min.css",
    "codemirror-python.min.js": "https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.12/mode/python/python.min.js",
    "codemirror-javascript.min.js": "https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.12/mode/javascript/javascript.min.js",
    "vis-network.min.js": "https://unpkg.com/vis-network/standalone/umd/vis-network.min.js",
}

VENDOR_DIR = Path("app/static/vendor")
MANIFEST_PATH = VENDOR_DIR / ".vendor_manifest.json"

async def ensure_local_assets(force_refresh: bool = False):
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for filename, url in REQUIRED_ASSETS.items():
            target_path = VENDOR_DIR / filename
            if not target_path.exists() or force_refresh:
                logger.info(f"Downloading vendor asset: {filename}...")
                resp = await client.get(url)
                resp.raise_for_status()
                target_path.write_bytes(resp.content)