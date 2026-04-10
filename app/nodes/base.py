from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseNode(ABC):
    """
    Base class for all Workflow Architect nodes.
    A self-contained node provides both its frontend representation
    and its backend execution logic.
    """
    node_type: str = ""

    @classmethod
    def get_frontend_js(cls) -> str:
        """Returns the Javascript code to register this node in LiteGraph."""
        return ""

    @abstractmethod
    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        """
        Executes the backend logic for this node.
        `engine` is the WorkflowEngine instance.
        """
        pass
