"""
Smart Priority Grouper
Groups models intelligently to represent all LLM categories while maintaining per-provider sorting
"""
import logging
from typing import List, Dict, Any, Set, Tuple
from app.database.models import ModelMetadata

logger = logging.getLogger(__name__)


# Define LLM categories we want to represent
LLM_CATEGORIES = {
    "general_chat": {"keywords": ["chat", "conversation", "general"], "priority": 1},
    "code": {"keywords": ["code", "coder", "coding", "programming"], "priority": 2},
    "reasoning": {"keywords": ["reasoning", "think", "thinking", "reason"], "priority": 3},
    "vision": {"keywords": ["vision", "image", "multimodal", "visual"], "priority": 4},
    "fast": {"keywords": ["fast", "turbo", "quick", "speed"], "priority": 5},
    "large_context": {"keywords": ["long", "context", "extended"], "priority": 6},
    "specialized": {"keywords": ["specialized", "domain", "specific"], "priority": 7},
}


def categorize_model(metadata: ModelMetadata, model_details: Dict[str, Any] = None) -> List[str]:
    """
    Categorize a model based on its capabilities and name.
    
    Returns:
        List of category names this model belongs to
    """
    if model_details is None:
        model_details = {}
    
    categories = []
    model_name_lower = metadata.model_name.lower()
    description_lower = (metadata.description or "").lower()
    combined_text = f"{model_name_lower} {description_lower}"
    
    # Check metadata flags
    if metadata.supports_images:
        categories.append("vision")
    
    if metadata.is_code_model:
        categories.append("code")
    
    if metadata.is_fast_model:
        categories.append("fast")
    
    # Check context length
    context_length = model_details.get("context_length", 0)
    if context_length >= 100000:
        categories.append("large_context")
    
    # Check name and description for keywords
    for category, info in LLM_CATEGORIES.items():
        if category not in categories:  # Don't duplicate
            keywords = info["keywords"]
            if any(keyword in combined_text for keyword in keywords):
                categories.append(category)
    
    # Default to general_chat if no categories found
    if not categories:
        categories.append("general_chat")
    
    return categories


def calculate_capability_score(
    metadata: ModelMetadata,
    model_details: Dict[str, Any] = None
) -> float:
    """
    Calculate a capability score for a model (higher = more capable).
    
    This considers:
    - Parameter size
    - Context length
    - Special capabilities (vision, code, reasoning)
    - Model family/architecture
    """
    if model_details is None:
        model_details = {}
    
    score = 0.0
    
    # Parameter size (larger = more capable, but also slower)
    param_size = model_details.get("parameter_size_num", 0)
    if param_size >= 70:
        score += 10.0
    elif param_size >= 34:
        score += 7.0
    elif param_size >= 20:
        score += 5.0
    elif param_size >= 10:
        score += 3.0
    elif param_size >= 3:
        score += 1.0
    
    # Context length (longer = more capable for many tasks)
    context_length = model_details.get("context_length", 0)
    if context_length >= 200000:
        score += 5.0
    elif context_length >= 100000:
        score += 3.0
    elif context_length >= 50000:
        score += 2.0
    elif context_length >= 32000:
        score += 1.0
    
    # Special capabilities
    if metadata.supports_images:
        score += 3.0
    
    if metadata.is_code_model:
        score += 2.0
    
    # Check for reasoning/thinking capabilities
    model_name_lower = metadata.model_name.lower()
    if any(kw in model_name_lower for kw in ["think", "reasoning", "reason", "deepseek-r1", "qwen"]):
        score += 2.5
    
    # Model family bonuses (premium families)
    family = model_details.get("family", "").lower()
    if any(f in family for f in ["gpt-4", "claude-3", "claude-4", "gemini-pro", "o1", "o3"]):
        score += 3.0
    elif any(f in family for f in ["gpt-3.5", "claude-2", "gemini"]):
        score += 1.5
    
    return score


def group_models_by_priority(
    selected_models: List[ModelMetadata],
    model_details_map: Dict[str, Dict[str, Any]],
    provider: str = None
) -> List[Tuple[int, List[ModelMetadata]]]:
    """
    Group selected models into priority tiers.
    
    Groups models to ensure all LLM categories are represented in the first group,
    then subsequent groups represent the next tier of models.
    
    Args:
        selected_models: List of selected ModelMetadata objects
        model_details_map: Dict mapping model_name -> details
        provider: Optional provider filter ("ollama", "openrouter", etc.)
    
    Returns:
        List of tuples: (priority, [models])
    """
    # Note: Models should already be filtered by provider before calling this function
    # The provider parameter is kept for clarity but filtering is done upstream
    
    if not selected_models:
        return []
    
    # Calculate capability scores and categorize
    model_scores = {}
    model_categories = {}
    
    for metadata in selected_models:
        model_name = metadata.model_name
        details = model_details_map.get(model_name, {})
        model_scores[model_name] = calculate_capability_score(metadata, details)
        model_categories[model_name] = categorize_model(metadata, details)
    
    # Sort by capability score (descending)
    sorted_models = sorted(
        selected_models,
        key=lambda m: model_scores.get(m.model_name, 0),
        reverse=True
    )
    
    # Group 1: Best models representing all categories
    group1_models = []
    covered_categories = set()
    remaining_models = sorted_models.copy()
    
    # First pass: ensure each category is represented
    for category in LLM_CATEGORIES.keys():
        if category in covered_categories:
            continue
        
        # Find best model for this category
        best_model = None
        best_score = -1
        
        for model in remaining_models:
            model_name = model.model_name
            categories = model_categories.get(model_name, [])
            if category in categories:
                score = model_scores.get(model_name, 0)
                if score > best_score:
                    best_score = score
                    best_model = model
        
        if best_model:
            group1_models.append(best_model)
            remaining_models.remove(best_model)
            # Mark all categories this model covers
            for cat in model_categories.get(best_model.model_name, []):
                covered_categories.add(cat)
    
    # Second pass: add top remaining models to fill out group 1
    # Aim for 5-8 models in group 1 to have good coverage
    target_group1_size = max(5, len(LLM_CATEGORIES))
    while len(group1_models) < target_group1_size and remaining_models:
        group1_models.append(remaining_models.pop(0))
    
    # Remaining models go into subsequent groups
    priority_groups = [(1, group1_models)]
    current_priority = 2
    
    # Group remaining models by capability tiers
    # Group by similar capability scores
    while remaining_models:
        if not remaining_models:
            break
        
        # Get the score of the first model in remaining
        first_score = model_scores.get(remaining_models[0].model_name, 0)
        
        # Group models with similar scores (within 2.0 points)
        group_models = []
        for model in remaining_models[:]:
            score = model_scores.get(model.model_name, 0)
            if abs(score - first_score) <= 2.0:
                group_models.append(model)
                remaining_models.remove(model)
        
        # If no models matched the score range, just take the first one
        if not group_models and remaining_models:
            group_models = [remaining_models.pop(0)]
        
        if group_models:
            priority_groups.append((current_priority, group_models))
            current_priority += 1
    
    return priority_groups


def assign_priorities_mixed_providers(
    ollama_models: List[ModelMetadata],
    openrouter_models: List[ModelMetadata],
    model_details_map: Dict[str, Dict[str, Any]]
) -> Dict[str, int]:
    """
    Assign priorities to models from multiple providers, allowing mixed priority groups.
    
    Models are sorted per-provider but can share priority numbers across providers.
    
    Args:
        ollama_models: List of Ollama ModelMetadata objects
        openrouter_models: List of OpenRouter ModelMetadata objects
        model_details_map: Dict mapping model_name -> details
    
    Returns:
        Dict mapping model_name -> priority
    """
    # Group each provider separately
    ollama_groups = group_models_by_priority(ollama_models, model_details_map, "ollama")
    openrouter_groups = group_models_by_priority(openrouter_models, model_details_map, "openrouter")
    
    # Merge groups: models from different providers can share priority numbers
    # Priority 1: Best from both providers
    # Priority 2: Next tier from both providers
    # etc.
    
    priorities = {}
    max_groups = max(len(ollama_groups), len(openrouter_groups))
    
    for group_num in range(1, max_groups + 1):
        priority = group_num
        
        # Add Ollama models from this group
        if group_num <= len(ollama_groups):
            _, ollama_group = ollama_groups[group_num - 1]
            for model in ollama_group:
                priorities[model.model_name] = priority
        
        # Add OpenRouter models from this group
        if group_num <= len(openrouter_groups):
            _, openrouter_group = openrouter_groups[group_num - 1]
            for model in openrouter_group:
                priorities[model.model_name] = priority
    
    return priorities

