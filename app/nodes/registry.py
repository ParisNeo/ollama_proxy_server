import importlib
import pkgutil
import inspect
import logging
import sys
from typing import Dict, Type
from app.nodes.base import BaseNode

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
        if not cls._loaded: cls.load_all()
        js_codes = []
        for n_cls in cls._nodes.values():
            js = n_cls.get_frontend_js()
            if js:
                js_codes.append(js)
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
        
        try:
            import app.nodes.standard
            import app.nodes.standard.utility_nodes # Explicitly include new file
            import app.nodes.custom
            
            for package in [app.nodes.standard, app.nodes.custom]:
                for _, module_name, _ in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
                    try:
                        # Force reload to ensure code changes in standard nodes are reflected
                        if module_name in sys.modules:
                            importlib.reload(sys.modules[module_name])
                        mod = importlib.import_module(module_name)
                        for attr_name in dir(mod):
                            attr = getattr(mod, attr_name)
                            if inspect.isclass(attr) and issubclass(attr, BaseNode) and attr is not BaseNode:
                                cls.register(attr)
                    except Exception as e:
                        logger.error(f"Failed to load node module {module_name}: {e}")
        except Exception as e:
            logger.error(f"Error discovering node packages: {e}")
