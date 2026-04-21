import importlib
import pkgutil
import inspect
import logging
import sys
from typing import Dict, Type
from app.nodes.base import BaseNode
from pathlib import Path

logger = logging.getLogger(__name__)

class NodeRegistry:
    _nodes: Dict[str, Type[BaseNode]] = {}
    _loaded = False
    
    @classmethod
    def register(cls, node_class: Type[BaseNode]):
        if node_class.node_type:
            is_new = node_class.node_type not in cls._nodes
            cls._nodes[node_class.node_type] = node_class
            if is_new:
                logger.info(f"Registered workflow node: {node_class.node_type}")
            
    @classmethod
    def get_node(cls, node_type: str) -> Type[BaseNode]:
        if not cls._loaded: cls.load_all()
        return cls._nodes.get(node_type)
        
    @classmethod
    def get_all_js(cls) -> str:
        """Collects JS from paired .js files and legacy class methods."""
        if not cls._loaded: cls.load_all()
        js_codes = []
        
        # 1. Scan directories RECURSIVELY for physical .js files
        import app.nodes.standard
        import app.nodes.custom
        from pathlib import Path

        for package in [app.nodes.standard, app.nodes.custom]:
            pkg_path = Path(package.__path__[0])
            for js_file in pkg_path.rglob("*.js"):
                try:
                    js_codes.append(js_file.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.error(f"Failed to read JS file {js_file}: {e}")

        # 2. Fallback: Check if any loaded class still uses the legacy method
        for n_cls in cls._nodes.values():
            try:
                # Only add if it's not a default empty implementation
                js = n_cls.get_frontend_js()
                if js and "LiteGraph.registerNodeType" in js:
                    # To prevent double registration if both exist, 
                    # we only add if this specific string isn't already in our collection
                    if not any(js[:50] in existing for existing in js_codes):
                        js_codes.append(js)
            except: pass

        return "\n\n".join(js_codes)

    @classmethod
    def get_node_list(cls):
        if not cls._loaded: cls.load_all()
        return [
            {
                "type": n.node_type,
                "title": n.node_title,
                "category": n.node_category,
                "icon": n.node_icon
            }
            for n in cls._nodes.values()
        ]

    @classmethod
    def load_all(cls):
        if cls._loaded: return
        cls._loaded = True
        cls._nodes = {}
        
        # Robust path discovery relative to this file's location
        base_pkg = Path(__file__).parent
        
        # Recursively find all python files in standard and custom
        for py_path in base_pkg.rglob("*.py"):
            if py_path.name == "__init__.py":
                continue
            
            # Skip if file is inside a template folder or other non-node directory
            if "templates" in py_path.parts:
                continue

            # Convert Path to dot-notation for import
            # We must ensure the path starts with 'app.nodes' regardless of where we are launched
            parts = list(py_path.with_suffix("").parts)
            try:
                # Find the 'app' index to normalize the module path
                idx = parts.index("app")
                module_name = ".".join(parts[idx:])
            except ValueError:
                # Fallback: manually prepend if 'app' isn't in parts (local dev scenario)
                module_name = "app.nodes." + ".".join(py_path.relative_to(base_pkg).with_suffix("").parts)
            
            try:
                if module_name in sys.modules:
                    importlib.reload(sys.modules[module_name])
                mod = importlib.import_module(module_name)
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    # Register classes that inherit from BaseNode
                    if inspect.isclass(attr) and issubclass(attr, BaseNode) and attr is not BaseNode:
                        cls.register(attr)
            except Exception as e:
                logger.error(f"Registry load failure for {module_name}: {e}")

