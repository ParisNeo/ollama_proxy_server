"""
Priority Assignment Modes for Auto Model Routing
Four modes: Free Mode, Daily Drive Mode, Advanced Mode, and Luxury Mode
"""
import logging
from typing import List, Dict, Any
from app.database.models import ModelMetadata

logger = logging.getLogger(__name__)


def is_free_model(metadata: ModelMetadata, model_details_map: Dict[str, Dict[str, Any]] = None) -> bool:
    """Check if a model is free based on pricing_summary or model_details_map."""
    model_name = metadata.model_name
    
    # First check pricing_summary if available (from template context)
    if hasattr(metadata, 'pricing_summary') and metadata.pricing_summary:
        pricing = metadata.pricing_summary
        input_price = pricing.get("input", "")
        output_price = pricing.get("output", "")
        
        # Check if both are "Free" or "$0" or similar
        input_free = isinstance(input_price, str) and ("free" in input_price.lower() or input_price == "$0" or input_price == "0")
        output_free = isinstance(output_price, str) and ("free" in output_price.lower() or output_price == "$0" or output_price == "0")
        
        if input_free and output_free:
            return True
    
    # Fallback: check model_details_map for pricing
    if model_details_map:
        details = model_details_map.get(model_name, {})
        pricing = details.get("pricing", {})
        
        if pricing:
            prompt_price = pricing.get("prompt", 0)
            completion_price = pricing.get("completion", 0)
            
            # Convert to float if string
            try:
                prompt_price = float(prompt_price) if prompt_price else 0
                completion_price = float(completion_price) if completion_price else 0
            except (ValueError, TypeError):
                prompt_price = 0
                completion_price = 0
            
            # Check if both are free (0 or very close to 0)
            if prompt_price == 0 and completion_price == 0:
                return True
    
    # Also check model name for "free" indicator
    if ":free" in model_name.lower() or model_name.lower().endswith(":free"):
        return True
    
    return False


def is_ollama_cloud_model(model_name: str) -> bool:
    """Check if a model is an Ollama cloud model (ends with :cloud)."""
    return model_name.endswith(":cloud")


def is_top_tier_model(model_name: str, metadata: ModelMetadata) -> bool:
    """Check if a model is top-tier (Claude 4.5, GPT-5, Gemini 3, etc.)."""
    model_name_lower = model_name.lower()
    
    # Top-tier model patterns
    top_tier_patterns = [
        "claude-4.5",
        "claude-4",
        "gpt-5",
        "gpt-5.1",
        "gemini-3",
        "gemini-3-pro",
        "o4",
        "o4-mini",
        "o4-mini-high",
    ]
    
    return any(pattern in model_name_lower for pattern in top_tier_patterns)


def is_mid_tier_model(model_name: str, metadata: ModelMetadata, model_details_map: Dict[str, Dict[str, Any]] = None) -> bool:
    """Check if a model is mid-tier (Opus-level and comparably priced)."""
    model_name_lower = model_name.lower()
    
    # Mid-tier model patterns (Opus-level)
    mid_tier_patterns = [
        "claude-opus",
        "claude-sonnet",
        "gpt-4",
        "gpt-4.1",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ]
    
    # Check pricing - Opus-level is typically $1-6 per 1M input tokens
    # First check pricing_summary if available
    if hasattr(metadata, 'pricing_summary') and metadata.pricing_summary:
        pricing = metadata.pricing_summary
        input_price = pricing.get("input", "")
        
        # Try to extract numeric price
        if isinstance(input_price, str):
            # Look for price patterns like "$3.00 / 1M tokens"
            import re
            price_match = re.search(r'\$?([\d.]+)', input_price)
            if price_match:
                try:
                    price = float(price_match.group(1))
                    # Opus-level pricing: roughly $1-6 per 1M input tokens
                    if 1.0 <= price <= 6.0:
                        return True
                except ValueError:
                    pass
    
    # Fallback: check model_details_map
    if model_details_map:
        details = model_details_map.get(model_name, {})
        pricing = details.get("pricing", {})
        if pricing:
            prompt_price = pricing.get("prompt", 0)
            try:
                prompt_price = float(prompt_price) if prompt_price else 0
                # Opus-level pricing: roughly $1-6 per 1M input tokens
                if 1.0 <= prompt_price <= 6.0:
                    return True
            except (ValueError, TypeError):
                pass
    
    return any(pattern in model_name_lower for pattern in mid_tier_patterns)


def assign_priorities_free_mode(
    ollama_models: List[ModelMetadata],
    openrouter_models: List[ModelMetadata],
    model_details_map: Dict[str, Dict[str, Any]]
) -> Dict[str, int]:
    """
    Free Mode: Prioritize free models first, then Ollama cloud, then paid.
    Priority 1: Free models
    Priority 2: Ollama cloud models
    Priority 3: Paid models
    """
    priorities = {}
    
    # Group all models
    all_models = ollama_models + openrouter_models
    
    # Separate into categories
    free_models = []
    cloud_models = []
    paid_models = []
    
    for metadata in all_models:
        model_name = metadata.model_name
        
        if is_free_model(metadata, model_details_map):
            free_models.append(metadata)
        elif is_ollama_cloud_model(model_name):
            cloud_models.append(metadata)
        else:
            paid_models.append(metadata)
    
    # Assign priorities
    priority = 1
    for model in free_models:
        priorities[model.model_name] = priority
    
    if cloud_models:
        priority = 2
        for model in cloud_models:
            priorities[model.model_name] = priority
    
    if paid_models:
        priority = 3
        for model in paid_models:
            priorities[model.model_name] = priority
    
    logger.info(f"Free Mode: {len(free_models)} free (P1), {len(cloud_models)} cloud (P2), {len(paid_models)} paid (P3)")
    
    return priorities


def assign_priorities_daily_drive_mode(
    ollama_models: List[ModelMetadata],
    openrouter_models: List[ModelMetadata],
    model_details_map: Dict[str, Dict[str, Any]]
) -> Dict[str, int]:
    """
    Daily Drive Mode: Prioritize Ollama cloud first, then free, then paid.
    Priority 1: Ollama cloud models
    Priority 2: Free models
    Priority 3: Paid models
    """
    priorities = {}
    
    # Group all models
    all_models = ollama_models + openrouter_models
    
    # Separate into categories
    cloud_models = []
    free_models = []
    paid_models = []
    
    for metadata in all_models:
        model_name = metadata.model_name
        
        if is_ollama_cloud_model(model_name):
            cloud_models.append(metadata)
        elif is_free_model(metadata, model_details_map):
            free_models.append(metadata)
        else:
            paid_models.append(metadata)
    
    # Assign priorities
    priority = 1
    for model in cloud_models:
        priorities[model.model_name] = priority
    
    if free_models:
        priority = 2
        for model in free_models:
            priorities[model.model_name] = priority
    
    if paid_models:
        priority = 3
        for model in paid_models:
            priorities[model.model_name] = priority
    
    logger.info(f"Daily Drive Mode: {len(cloud_models)} cloud (P1), {len(free_models)} free (P2), {len(paid_models)} paid (P3)")
    
    return priorities


def assign_priorities_advanced_mode(
    ollama_models: List[ModelMetadata],
    openrouter_models: List[ModelMetadata],
    model_details_map: Dict[str, Dict[str, Any]]
) -> Dict[str, int]:
    """
    Advanced Mode: Prioritize top-tier paid models, then mid-tier paid models.
    Skip free models entirely.
    Priority 1: Top-tier models (Claude 4.5, GPT-5, Gemini 3, etc.)
    Priority 2: Mid-tier models (Opus-level and comparably priced)
    """
    priorities = {}
    
    # Group all models
    all_models = ollama_models + openrouter_models
    
    # Separate into categories (skip free models)
    top_tier_models = []
    mid_tier_models = []
    other_paid_models = []
    
    for metadata in all_models:
        model_name = metadata.model_name
        
        # Skip free models
        if is_free_model(metadata, model_details_map):
            continue
        
        if is_top_tier_model(model_name, metadata):
            top_tier_models.append(metadata)
        elif is_mid_tier_model(model_name, metadata, model_details_map):
            mid_tier_models.append(metadata)
        else:
            # Other paid models (not top or mid tier)
            other_paid_models.append(metadata)
    
    # Assign priorities
    priority = 1
    for model in top_tier_models:
        priorities[model.model_name] = priority
    
    if mid_tier_models:
        priority = 2
        for model in mid_tier_models:
            priorities[model.model_name] = priority
    
    # Optionally assign other paid models to priority 3
    if other_paid_models:
        priority = 3
        for model in other_paid_models:
            priorities[model.model_name] = priority
    
    logger.info(f"Advanced Mode: {len(top_tier_models)} top-tier (P1), {len(mid_tier_models)} mid-tier (P2), {len(other_paid_models)} other paid (P3)")
    
    return priorities


def assign_priorities_luxury_mode(
    ollama_models: List[ModelMetadata],
    openrouter_models: List[ModelMetadata],
    model_details_map: Dict[str, Dict[str, Any]]
) -> Dict[str, int]:
    """
    Luxury Mode: Prioritize premium models (Opus-level and higher) for high-budget scenarios.
    Budget-conscious routing that considers cost but prioritizes quality.
    Priority 1: Top-tier premium models (Claude 4.5, GPT-5, Gemini 3 Pro, O4, etc.) - $5+/1M tokens
    Priority 2: Mid-tier premium models (Opus-level: Claude Opus, GPT-4, Gemini 2.5 Pro) - $1-5/1M tokens
    Priority 3: Other paid models (lower cost but still quality)
    Free models are skipped entirely.
    """
    priorities = {}
    
    # Group all models
    all_models = ollama_models + openrouter_models
    
    # Separate into categories (skip free models)
    top_tier_premium = []  # $5+ per 1M tokens
    mid_tier_premium = []  # $1-5 per 1M tokens (Opus-level)
    other_paid = []  # <$1 per 1M tokens
    
    for metadata in all_models:
        model_name = metadata.model_name
        
        # Skip free models
        if is_free_model(metadata, model_details_map):
            continue
        
        # Get pricing
        prompt_price = 0
        if model_details_map:
            details = model_details_map.get(model_name, {})
            pricing = details.get("pricing", {})
            if pricing:
                try:
                    prompt_price = float(pricing.get("prompt", 0)) if pricing.get("prompt") else 0
                except (ValueError, TypeError):
                    prompt_price = 0
        
        # Categorize by pricing and model tier
        if is_top_tier_model(model_name, metadata) or prompt_price >= 5.0:
            top_tier_premium.append(metadata)
        elif is_mid_tier_model(model_name, metadata, model_details_map) or (1.0 <= prompt_price < 5.0):
            mid_tier_premium.append(metadata)
        else:
            other_paid.append(metadata)
    
    # Assign priorities
    priority = 1
    for model in top_tier_premium:
        priorities[model.model_name] = priority
    
    if mid_tier_premium:
        priority = 2
        for model in mid_tier_premium:
            priorities[model.model_name] = priority
    
    if other_paid:
        priority = 3
        for model in other_paid:
            priorities[model.model_name] = priority
    
    logger.info(f"Luxury Mode: {len(top_tier_premium)} top-tier premium (P1), {len(mid_tier_premium)} mid-tier premium (P2), {len(other_paid)} other paid (P3)")
    
    return priorities

