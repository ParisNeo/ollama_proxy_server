import os
import httpx
from pathlib import Path
from typing import Dict, Any, List
from app.nodes.base import BaseNode
from app.core import knowledge_importer as kit
from fastapi.concurrency import run_in_threadpool

class FileReaderNode(BaseNode):
    node_type = "hub/file_reader"
    node_title = "File Reader"
    node_category = "Data Loaders"
    node_icon = "📄"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        # Input 0 can override the property path
        path_override = await engine._resolve_input(node, 0)
        file_path = str(path_override) if path_override else node["properties"].get("path", "")
        
        if not file_path or not os.path.exists(file_path):
            return f"[Error: File not found at {file_path}]"

        try:
            # Use the existing kit to handle PDF/Docx/Text automatically
            from fastapi import UploadFile
            # We simulate an UploadFile for the kit helper
            async def _read():
                with open(file_path, "rb") as f:
                    content = f.read()
                
                # Check extension
                ext = os.path.splitext(file_path)[1].lower()
                if ext in ('.pdf', '.docx'):
                    # Create a dummy class to satisfy the kit's expectation of an UploadFile object
                    class MockFile:
                        def __init__(self, name, data):
                            self.filename = name
                            self.content_type = "application/octet-stream"
                            self._data = data
                        async def read(self): return self._data
                    
                    return await kit.extract_local_file_content([MockFile(file_path, content)])
                else:
                    return content.decode('utf-8', errors='ignore')

            return await _read()
        except Exception as e:
            return f"[Read Error: {str(e)}]"

class WebLoaderNode(BaseNode):
    node_type = "hub/web_loader"
    node_title = "URL Scraper"
    node_category = "Data Loaders"
    node_icon = "🌐"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        url = await engine._resolve_input(node, 0) or node["properties"].get("url", "")
        if not url: return ""

        try:
            # Re-use the kit's scrape logic (handles content extraction)
            res = await kit.scrape_url(str(url))
            return f"TITLE: {res.get('title')}\n\nCONTENT:\n{res.get('content')}"
        except Exception as e:
            return f"[Scrape Error: {str(e)}]"

class DirectoryScannerNode(BaseNode):
    node_type = "hub/dir_scanner"
    node_title = "Directory Scanner"
    node_category = "Data Loaders"
    node_icon = "📁"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        dir_path = node["properties"].get("path", "")
        filter_ext = node["properties"].get("extensions", "*")
        
        if not dir_path or not os.path.isdir(dir_path):
            return []

        files = []
        ext_list = [e.strip().lower() for e in filter_ext.split(",") if e.strip()]
        
        for f in os.listdir(dir_path):
            f_path = os.path.join(dir_path, f)
            if os.path.isfile(f_path):
                if "*" in ext_list or any(f.lower().endswith(e) for e in ext_list):
                    files.append(f_path)
        
        return sorted(files)