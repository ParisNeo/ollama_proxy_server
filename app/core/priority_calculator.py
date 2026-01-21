"""
Automatic priority calculation for AI models based on capabilities, price, and descriptions.
Free models are prioritized first, then paid models by capability strength and cost.
"""
import logging
from typing import List, Dict, Any, Optional
from app.database.models import ModelMetadata

logger = logging.getLogger(__name__)


def calculate_model_priority(
    metadata: ModelMetadata,
    model_details: Dict[str, Any] = None,
    all_models: List[ModelMetadata] = None
) -> int:
    """
    Calculate priority for a model based on:
    - Free vs paid (free models get priority 1-5, paid get 6-10)
    - Capability strength (images, code, fast inference)
    - Context length
    - Price (for paid models)
    - Description quality
    
    Lower priority number = higher priority (used first)
    
    Args:
        metadata: ModelMetadata object
        model_details: Optional dict with model details (context_length, pricing, etc.)
        all_models: Optional list of all models for relative comparison
    
    Returns:
        Priority value (1-10, where 1 is highest priority)
    """
    if model_details is None:
        model_details = {}
    
    model_name = metadata.model_name.lower()
    
    # Check if free model
    is_free = (
        "free" in model_name or
        model_details.get("is_free", False) or
        (model_details.get("pricing", {}).get("prompt", 1) == 0 and 
         model_details.get("pricing", {}).get("completion", 1) == 0)
    )
    
    # Base priority: Free models get 1-5, paid get 6-10
    if is_free:
        base_priority = 1  # Start at 1 for free models
    else:
        base_priority = 6  # Start at 6 for paid models
    
    # Calculate capability score (higher = better)
    capability_score = 0
    
    # Image support is valuable
    if metadata.supports_images:
        capability_score += 3
    
    # Code model capability
    if metadata.is_code_model:
        capability_score += 2
    
    # Fast inference
    if metadata.is_fast_model:
        capability_score += 1
    
    # Context length bonus (longer context = better for most use cases)
    context_length = model_details.get("context_length", 0)
    if context_length:
        if context_length >= 200000:
            capability_score += 2
        elif context_length >= 100000:
            capability_score += 1
        elif context_length >= 32000:
            capability_score += 0.5
    
    # Parameter size (larger models often more capable, but slower)
    parameter_size = model_details.get("parameter_size_num", 0)
    if parameter_size:
        if parameter_size >= 70:
            capability_score += 1.5
        elif parameter_size >= 34:
            capability_score += 1
        elif parameter_size >= 20:
            capability_score += 0.5
    
    # Description quality bonus (if description exists and is meaningful)
    if metadata.description and len(metadata.description.strip()) > 20:
        capability_score += 0.5
    
    # For paid models, factor in price (lower price = higher priority within same capability tier)
    price_adjustment = 0
    if not is_free:
        pricing = model_details.get("pricing", {})
        prompt_price = pricing.get("prompt", 0)
        completion_price = pricing.get("completion", 0)
        # Convert to float if string
        try:
            prompt_price = float(prompt_price) if prompt_price else 0
        except (ValueError, TypeError):
            prompt_price = 0
        try:
            completion_price = float(completion_price) if completion_price else 0
        except (ValueError, TypeError):
            completion_price = 0
        avg_price = (prompt_price + completion_price) / 2
        
        # Lower price = better (subtract from priority)
        if avg_price > 0:
            if avg_price < 1.0:  # Very cheap (< $1 per million tokens)
                price_adjustment = -0.5
            elif avg_price < 5.0:  # Cheap (< $5 per million tokens)
                price_adjustment = -0.25
            elif avg_price > 50.0:  # Expensive (> $50 per million tokens)
                price_adjustment = 0.5
    
    # Calculate final priority
    # Lower capability_score means higher priority, so we subtract it
    # But we want to keep it within bounds
    priority = base_priority - (capability_score / 2) + price_adjustment
    
    # Clamp to valid range (1-10)
    priority = max(1, min(10, int(round(priority))))
    
    return priority


def calculate_priorities_for_all_models(
    all_metadata: List[ModelMetadata],
    model_details_map: Dict[str, Dict[str, Any]] = None
) -> Dict[str, int]:
    """
    Calculate priorities for all models and return a mapping of model_name -> priority.
    
    Args:
        all_metadata: List of all ModelMetadata objects
        model_details_map: Optional dict mapping model_name -> details dict
    
    Returns:
        Dict mapping model_name -> priority value
    """
    if model_details_map is None:
        model_details_map = {}
    
    priorities = {}
    
    for metadata in all_metadata:
        model_name = metadata.model_name
        details = model_details_map.get(model_name, {})
        priority = calculate_model_priority(metadata, details, all_metadata)
        priorities[model_name] = priority
    
    return priorities

