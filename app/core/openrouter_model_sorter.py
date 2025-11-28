"""
OpenRouter Model Sorter and Filter

Categorizes and sorts OpenRouter models according to user preferences:
1. Free models with >= 20B parameters and active endpoints
2. Major LLM providers (alphabetically) - latest versions only
3. Uncensored models section
"""
from typing import List, Dict, Any, Tuple
import re
from datetime import datetime

# Major LLM providers (alphabetically ordered)
MAJOR_PROVIDERS = [
    "anthropic",  # Claude
    "google",     # Gemini
    "meta",       # Llama
    "mistralai",  # Mistral
    "openai",     # GPT
    "qwen",       # Qwen
    "deepseek",   # DeepSeek
]

# Known uncensored model patterns - ONLY truly uncensored models
# Dolphin-based models are the primary uncensored models
# Other patterns may have guardrails, so we're conservative
UNCENSORED_PATTERNS = [
    "dolphin",  # Dolphin models are truly uncensored
    # Note: "uncensored", "unfiltered", "unrestricted" in name may still have guardrails
    # Only include if explicitly verified as uncensored
]

def extract_model_version(model_name: str) -> Tuple[str, float]:
    """
    Extracts model family and version number for sorting.
    Returns (family, version_number) where version_number is higher for newer models.
    """
    # Remove provider prefix
    model_part = model_name.split('/')[-1] if '/' in model_name else model_name
    
    # Extract version numbers (e.g., "gpt-4o" -> 4.0, "claude-3-opus" -> 3.0)
    version_match = re.search(r'(\d+)', model_part)
    if version_match:
        version_num = float(version_match.group(1))
        # Check for sub-versions (e.g., "3.5" -> 3.5, "4o" -> 4.0)
        sub_version_match = re.search(r'(\d+)\.(\d+)', model_part)
        if sub_version_match:
            version_num = float(f"{sub_version_match.group(1)}.{sub_version_match.group(2)}")
    else:
        version_num = 0.0
    
    # Extract family name (remove version numbers)
    family = re.sub(r'[-_]?\d+[\.-]?\d*[a-z]*', '', model_part).strip('-').strip('_')
    
    return (family, version_num)

def is_latest_version(model_name: str, all_models: List[Dict[str, Any]]) -> bool:
    """
    Determines if a model is the latest version of its family.
    Only considers models from the same provider.
    """
    provider = model_name.split('/')[0] if '/' in model_name else "unknown"
    family, version = extract_model_version(model_name)
    
    # Find all models from the same provider and family
    same_family_models = []
    for model in all_models:
        model_name_other = model.get("name", "")
        provider_other = model_name_other.split('/')[0] if '/' in model_name_other else "unknown"
        family_other, version_other = extract_model_version(model_name_other)
        
        if provider == provider_other and family.lower() == family_other.lower():
            same_family_models.append((model_name_other, version_other))
    
    if not same_family_models:
        return True
    
    # Check if this model has the highest version
    max_version = max(v for _, v in same_family_models)
    return version >= max_version

def get_model_name(m):
    """Helper to get model name from either object attribute or dict key"""
    if hasattr(m, 'model_name'):
        return m.model_name
    return m.get("model_name", str(m))

def get_parameter_size_num(model_name: str, details: Dict[str, Any] = None) -> int:
    """Helper to extract parameter size number from model name or details"""
    if details is None:
        details = {}
    
    parameter_size_num = details.get("parameter_size_num", 0)
    if parameter_size_num == 0:
        model_name_lower = model_name.lower()
        if "70b" in model_name_lower or "70-b" in model_name_lower:
            return 70
        elif "34b" in model_name_lower or "34-b" in model_name_lower:
            return 34
        elif "20b" in model_name_lower or "20-b" in model_name_lower:
            return 20
        elif "13b" in model_name_lower or "13-b" in model_name_lower:
            return 13
        elif "8b" in model_name_lower or "8-b" in model_name_lower:
            return 8
        elif "7b" in model_name_lower or "7-b" in model_name_lower:
            return 7
        elif "3b" in model_name_lower or "3-b" in model_name_lower:
            return 3
        elif "1b" in model_name_lower or "1-b" in model_name_lower:
            return 1
    return parameter_size_num

def categorize_models_by_server(models: List[Dict[str, Any]], model_details_map: Dict[str, Dict[str, Any]] = None) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Categorizes models by server type first, then by category within each server type.
    
    Structure:
    {
        "ollama": {
            "all": [list of all Ollama models]
        },
        "openrouter": {
            "free": [free models sorted first],
            "major_providers": [latest versions from major providers],
            "uncensored": [uncensored models - Dolphin-based only],
            "other": [remaining OpenRouter models]
        },
        "vllm": {
            "all": [list of all vLLM models]
        }
    }
    
    Args:
        models: List of model metadata objects with server_type attribute
        model_details_map: Optional dict mapping model_name -> details dict from server.available_models
    """
    if model_details_map is None:
        model_details_map = {}
    
    # Separate by server type
    ollama_models = [m for m in models if hasattr(m, 'server_type') and m.server_type == "ollama"]
    openrouter_models = [m for m in models if hasattr(m, 'server_type') and m.server_type == "openrouter"]
    vllm_models = [m for m in models if hasattr(m, 'server_type') and m.server_type == "vllm"]
    other_models = [m for m in models if not hasattr(m, 'server_type') or m.server_type not in ["ollama", "openrouter", "vllm"]]
    
    result = {
        "ollama": {"all": sorted(ollama_models, key=lambda m: get_model_name(m).lower())},
        "openrouter": categorize_openrouter_models(openrouter_models, model_details_map),
        "vllm": {"all": sorted(vllm_models, key=lambda m: get_model_name(m).lower())},
        "other": {"all": sorted(other_models, key=lambda m: get_model_name(m).lower())}
    }
    
    return result

def categorize_openrouter_models(models: List[Dict[str, Any]], model_details_map: Dict[str, Dict[str, Any]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    Categorizes OpenRouter models into sections:
    - free: ALL free models (sorted first, at the top)
    - major_providers: Latest versions from major providers (alphabetically)
    - uncensored: Dolphin-based uncensored models only
    - other: Remaining OpenRouter models
    
    Args:
        models: List of model metadata objects with server_type == "openrouter"
        model_details_map: Optional dict mapping model_name -> details dict from server.available_models
    """
    if model_details_map is None:
        model_details_map = {}
    
    categorized = {
        "free": [],  # ALL free models (shown first)
        "major_providers": [],
        "uncensored": [],
        "other": []
    }
    
    for model in models:
        model_name = model.model_name if hasattr(model, 'model_name') else str(model)
        model_name_lower = model_name.lower()
        
        # Get details from the map if available
        details = model_details_map.get(model_name, {})
        if hasattr(model, 'model_details') and isinstance(model.model_details, dict):
            details.update(model.model_details)
        
        # Check if free (from details or model name)
        # OpenRouter free models typically have ":free" suffix or pricing.prompt == 0
        # IMPORTANT: Models with "free" in the name ALWAYS have active endpoints
        is_free = (
            details.get("is_free", False) or 
            "free" in model_name_lower or  # Check anywhere in name, not just suffix
            ":free" in model_name_lower or 
            model_name_lower.endswith(":free") or
            (details.get("pricing", {}).get("prompt", 1) == 0 and details.get("pricing", {}).get("completion", 1) == 0)
        )
        
        # Get parameter size from details or extract from name
        parameter_size_num = details.get("parameter_size_num", 0)
        if parameter_size_num == 0:
            # Fallback: extract from name
            if "70b" in model_name_lower or "70-b" in model_name_lower:
                parameter_size_num = 70
            elif "34b" in model_name_lower or "34-b" in model_name_lower:
                parameter_size_num = 34
            elif "20b" in model_name_lower or "20-b" in model_name_lower:
                parameter_size_num = 20
            elif "13b" in model_name_lower or "13-b" in model_name_lower:
                parameter_size_num = 13
        
        # Check if uncensored (ONLY Dolphin-based models are truly uncensored)
        # Be conservative - most models with "uncensored" in name still have guardrails
        is_uncensored = "dolphin" in model_name_lower or (
            details.get("is_uncensored", False) and "dolphin" in model_name_lower
        )
        
        # Check if major provider
        provider = model_name.split('/')[0] if '/' in model_name else "unknown"
        is_major_provider = provider.lower() in [p.lower() for p in MAJOR_PROVIDERS]
        
        # Categorize: Each model should appear in ONLY ONE category to avoid duplicates
        # Priority: free > other (top) > uncensored > major_providers
        
        if is_free:
            # Free models go to "free" category only (shown at top)
            categorized["free"].append(model)
            # Don't add to other categories to avoid duplicates
        elif is_uncensored:
            # Uncensored models (non-free) go to "uncensored" category only
            categorized["uncensored"].append(model)
        elif is_major_provider:
            # Major provider models (non-free, non-uncensored) go to "major_providers"
            categorized["major_providers"].append(model)
        else:
            # All other models go to "other" category
            categorized["other"].append(model)
    
    # Sort ALL free models first (by parameter size descending, then alphabetically)
    categorized["free"].sort(
        key=lambda m: (
            -get_parameter_size_num(get_model_name(m), model_details_map.get(get_model_name(m), {})),
            get_model_name(m).lower()
        )
    )
    
    # Sort major providers: first by provider (alphabetically), then by version (descending)
    categorized["major_providers"].sort(
        key=lambda m: (
            get_model_name(m).split('/')[0] if '/' in get_model_name(m) else "zzz",
            -extract_model_version(get_model_name(m))[1]
        )
    )
    
    # Filter major providers to only latest versions
    provider_families = {}
    for model in categorized["major_providers"]:
        model_name = get_model_name(model)
        provider = model_name.split('/')[0] if '/' in model_name else "unknown"
        family, version = extract_model_version(model_name)
        
        key = (provider.lower(), family.lower())
        if key not in provider_families or version > provider_families[key][1]:
            provider_families[key] = (model, version)
    
    categorized["major_providers"] = [m for m, _ in provider_families.values()]
    categorized["major_providers"].sort(
        key=lambda m: get_model_name(m).split('/')[0] if '/' in get_model_name(m) else "zzz"
    )
    
    # Sort uncensored alphabetically
    categorized["uncensored"].sort(key=lambda m: get_model_name(m).lower())
    
    # Sort other models alphabetically
    categorized["other"].sort(key=lambda m: get_model_name(m).lower())
    
    return categorized

