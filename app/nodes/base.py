from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseNode(ABC):
    """
    Base class for all Workflow Architect nodes.
    Provides metadata for the sidebar and the execution interface.
    """
    node_type: str = ""
    node_title: str = "Unnamed Node"
    node_category: str = "Logic & Routing"
    node_icon: str = "🧩"

    @classmethod
    def get_frontend_js(cls) -> str:
        """Returns the Javascript code to register this node in LiteGraph."""
        return ""

    @abstractmethod
    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        """
        Executes the backend logic for this node.
        """
        pass