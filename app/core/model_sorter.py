"""
Model sorting utilities for Models Manager.

Sorts models by:
1. Capabilities (images, tool calling/coding, thinking/reasoning)
2. Context length (highest first)
3. Date published/updated (newest first)
"""
from typing import List, Dict, Any, Tuple
from datetime import datetime

def get_model_name(m):
    """Helper to get model name from either object attribute or dict key"""
    if hasattr(m, 'model_name'):
        return m.model_name
    elif isinstance(m, dict):
        return m.get("model_name", m.get("name", str(m)))
    return str(m)

def get_capability_score(model: Any, model_details: Dict[str, Any] = None) -> Tuple[int, int, int]:
    """
    Calculate capability scores for a model.
    Returns (image_score, tool_score, thinking_score) where higher is better.
    
    image_score: 1 if supports images, 0 otherwise
    tool_score: 2 if supports tools/coding, 1 if code model, 0 otherwise
    thinking_score: 2 if has thinking/reasoning, 1 if reasoning mentioned, 0 otherwise
    """
    if model_details is None:
        model_details = {}
    
    # Get metadata if available
    if hasattr(model, 'supports_images'):
        supports_images = model.supports_images
    elif hasattr(model, 'model_details') and isinstance(model.model_details, dict):
        supports_images = model.model_details.get("supports_images", False)
    else:
        supports_images = model_details.get("supports_images", False) or model_details.get("vision", False)
    
    # Tool/coding capability
    is_code_model = False
    supports_tools = False
    if hasattr(model, 'is_code_model'):
        is_code_model = model.is_code_model
    elif hasattr(model, 'model_details') and isinstance(model.model_details, dict):
        is_code_model = model.model_details.get("is_code_model", False)
        supports_tools = model.model_details.get("tools", False) or model.model_details.get("function_calling", False)
    else:
        is_code_model = model_details.get("is_code_model", False)
        supports_tools = model_details.get("tools", False) or model_details.get("function_calling", False)
    
    # Thinking/reasoning capability
    model_name = get_model_name(model).lower()
    has_thinking = "thinking" in model_name or "think" in model_name
    has_reasoning = "reasoning" in model_name or "reason" in model_name
    
    # Check description for reasoning mentions
    description = ""
    if hasattr(model, 'description'):
        description = (model.description or "").lower()
    elif hasattr(model, 'model_details') and isinstance(model.model_details, dict):
        description = (model.model_details.get("description", "") or "").lower()
    else:
        description = (model_details.get("description", "") or "").lower()
    
    has_reasoning_in_desc = "reasoning" in description or "thinking" in description or "chain-of-thought" in description
    
    # Calculate scores
    image_score = 1 if supports_images else 0
    tool_score = 2 if supports_tools else (1 if is_code_model else 0)
    thinking_score = 2 if (has_thinking or has_reasoning) else (1 if has_reasoning_in_desc else 0)
    
    return (image_score, tool_score, thinking_score)

def get_context_length(model: Any, model_details: Dict[str, Any] = None) -> int:
    """Get context length for a model, defaulting to 0 if not available."""
    if model_details is None:
        model_details = {}
    
    ctx_len = 0
    if hasattr(model, 'model_details') and isinstance(model.model_details, dict):
        ctx_len = model.model_details.get("context_length", 0)
    else:
        ctx_len = model_details.get("context_length", 0)
    
    return int(ctx_len) if ctx_len else 0

def get_model_date(model: Any, model_details: Dict[str, Any] = None) -> datetime:
    """Get model published/updated date, defaulting to earliest date if not available."""
    if model_details is None:
        model_details = {}
    
    # Try to get date from various sources
    date_str = None
    if hasattr(model, 'model_details') and isinstance(model.model_details, dict):
        date_str = model.model_details.get("created", None) or model.model_details.get("updated", None)
    else:
        date_str = model_details.get("created", None) or model_details.get("updated", None)
    
    if date_str:
        try:
            if isinstance(date_str, str):
                # Try parsing ISO format
                return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            elif isinstance(date_str, (int, float)):
                # Unix timestamp
                return datetime.fromtimestamp(date_str)
        except:
            pass
    
    # Default to earliest date for sorting (newest first means earliest goes last)
    return datetime.min

def sort_models_by_capabilities(models: List[Any], model_details_map: Dict[str, Dict[str, Any]] = None) -> List[Any]:
    """
    Sort models by capabilities, context length, and date.
    
    Sort order:
    1. Capability score (images + tools + thinking, highest first)
    2. Context length (highest first)
    3. Date (newest first)
    
    Note: Cloud model separation is handled at a higher level in admin.py
    """
    if model_details_map is None:
        model_details_map = {}
    
    def sort_key(m):
        model_name = get_model_name(m)
        details = model_details_map.get(model_name, {})
        
        # Get capability scores
        image_score, tool_score, thinking_score = get_capability_score(m, details)
        total_capability = image_score + tool_score + thinking_score
        
        # Get context length
        ctx_len = get_context_length(m, details)
        
        # Get date
        model_date = get_model_date(m, details)
        
        # Return tuple for sorting (negative for descending order)
        return (
            -total_capability,  # Highest capabilities first
            -ctx_len,           # Highest context first
            -model_date.timestamp() if model_date != datetime.min else 0  # Newest first
        )
    
    return sorted(models, key=sort_key)

