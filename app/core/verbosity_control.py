"""
Verbosity control for universal response length/detail management
Works with both OpenRouter and Ollama by adjusting system prompts and parameters
"""
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# Verbosity system prompts
VERBOSITY_PROMPTS = {
    "short": "Please provide concise, brief answers. Be direct and to the point. Avoid unnecessary elaboration or examples unless specifically requested.",
    "medium": "Please provide balanced, informative answers with appropriate detail. Include relevant context and examples when helpful, but avoid excessive verbosity.",
    "maximum": "Please provide comprehensive, detailed answers. Include thorough explanations, context, examples, and relevant details. Be expansive and thorough in your responses."
}

# Verbosity parameter mappings
VERBOSITY_PARAMS = {
    "short": {
        "max_tokens": 500,  # Lower token limit for concise responses
        "temperature": 0.3,  # Lower for more focused responses
    },
    "medium": {
        "max_tokens": 2000,  # Moderate token limit
        "temperature": 0.5,  # Balanced creativity
    },
    "maximum": {
        "max_tokens": None,  # No limit (or very high limit)
        "temperature": 0.6,  # Moderate for detailed but controlled responses
    }
}


def apply_verbosity_to_messages(
    messages: List[Dict[str, Any]],
    verbosity: str = "maximum"
) -> List[Dict[str, Any]]:
    """
    Apply verbosity control to messages by adding/updating system prompt.
    
    Args:
        messages: List of message dicts
        verbosity: "short", "medium", or "maximum"
    
    Returns:
        Updated messages list with verbosity system prompt
    """
    if verbosity not in VERBOSITY_PROMPTS:
        logger.warning(f"Unknown verbosity level: {verbosity}, defaulting to 'maximum'")
        verbosity = "maximum"
    
    verbosity_prompt = VERBOSITY_PROMPTS[verbosity]
    
    # Check if there's already a system message
    has_system = False
    for msg in messages:
        if msg.get("role") == "system":
            # Append verbosity instruction to existing system prompt
            existing_content = msg.get("content", "")
            if verbosity_prompt not in existing_content:
                msg["content"] = f"{existing_content}\n\n{verbosity_prompt}".strip()
            has_system = True
            break
    
    # If no system message exists, add one
    if not has_system:
        messages.insert(0, {
            "role": "system",
            "content": verbosity_prompt
        })
    
    return messages


def apply_verbosity_to_params(
    payload: Dict[str, Any],
    verbosity: str = "maximum"
) -> Dict[str, Any]:
    """
    Apply verbosity control to API parameters (max_tokens, temperature, etc.).
    
    Args:
        payload: API request payload
        verbosity: "short", "medium", or "maximum"
    
    Returns:
        Updated payload with verbosity parameters
    """
    if verbosity not in VERBOSITY_PARAMS:
        logger.warning(f"Unknown verbosity level: {verbosity}, defaulting to 'maximum'")
        verbosity = "maximum"
    
    verbosity_params = VERBOSITY_PARAMS[verbosity]
    
    # Apply max_tokens if not already set (or if verbosity wants to override)
    if "max_tokens" not in payload and verbosity_params["max_tokens"] is not None:
        payload["max_tokens"] = verbosity_params["max_tokens"]
    elif verbosity == "maximum" and "max_tokens" in payload:
        # For maximum verbosity, remove or set very high limit
        # Don't override if user explicitly set it
        pass
    
    # Adjust temperature slightly if not explicitly set
    if "temperature" not in payload:
        payload["temperature"] = verbosity_params["temperature"]
    else:
        # If temperature is set, we can still apply a slight adjustment
        # but be more conservative - only adjust if it's close to default
        current_temp = payload.get("temperature", 0.5)
        if 0.3 <= current_temp <= 0.6:
            # Slight adjustment based on verbosity
            if verbosity == "short":
                payload["temperature"] = min(current_temp, 0.35)
            elif verbosity == "maximum":
                payload["temperature"] = max(current_temp, 0.55)
    
    return payload

