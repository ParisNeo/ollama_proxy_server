"""
Pricing formatting utilities for displaying model costs
"""
from typing import Dict, Any, Optional

def format_price(price: float, per_unit: str = "1M tokens") -> str:
    """
    Format a price value for display
    
    Args:
        price: Price value (e.g., 0.00001 for $0.00001 per million tokens)
        per_unit: Unit description
        
    Returns:
        Formatted price string
    """
    if price == 0:
        return "Free"
    
    # Convert to dollars per million tokens
    if price < 0.001:
        # Show as cents per million tokens
        cents = price * 100000
        if cents < 1:
            return f"${price * 1000000:.2f} / {per_unit}"
        return f"${cents:.2f} / {per_unit}"
    elif price < 1:
        # Show as dollars with cents
        return f"${price:.4f} / {per_unit}"
    else:
        # Show as whole dollars
        return f"${price:.2f} / {per_unit}"

def format_web_search_price(web_search_price: float) -> str:
    """
    Format web search pricing (usually per 1K requests or per request)
    
    Args:
        web_search_price: Price value from OpenRouter
        
    Returns:
        Formatted price string
    """
    if web_search_price == 0:
        return "Included"
    
    # OpenRouter web_search is typically per 1K requests
    if web_search_price < 0.01:
        return f"${web_search_price * 1000:.2f} / 1K requests"
    elif web_search_price < 1:
        return f"${web_search_price:.4f} / request"
    else:
        return f"${web_search_price:.2f} / request"

def get_pricing_summary(pricing: Dict[str, Any]) -> Dict[str, str]:
    """
    Get a formatted pricing summary for a model
    
    Args:
        pricing: Pricing dict from OpenRouter API
        
    Returns:
        Dict with formatted price strings
    """
    # Handle string values from API (OpenRouter returns prices as strings)
    def safe_float(value, default=0):
        if isinstance(value, str):
            try:
                return float(value)
            except (ValueError, TypeError):
                return default
        return float(value) if value is not None else default
    
    prompt_price = safe_float(pricing.get("prompt", 0))
    completion_price = safe_float(pricing.get("completion", 0))
    web_search_price = safe_float(pricing.get("web_search", 0))
    request_price = safe_float(pricing.get("request", 0))
    image_price = safe_float(pricing.get("image", 0))
    
    summary = {
        "input": format_price(prompt_price),
        "output": format_price(completion_price),
    }
    
    if web_search_price > 0:
        summary["web_search"] = format_web_search_price(web_search_price)
    elif "web_search" in pricing:
        summary["web_search"] = "Included"
    
    if request_price > 0:
        summary["request"] = format_price(request_price, "request")
    
    if image_price > 0:
        summary["image"] = format_price(image_price, "image")
    
    return summary

def get_pricing_tooltip(pricing: Dict[str, Any], model_name: str = "") -> str:
    """
    Generate HTML tooltip content for pricing display
    
    Args:
        pricing: Pricing dict from OpenRouter API
        model_name: Optional model name for context
        
    Returns:
        HTML string for tooltip
    """
    summary = get_pricing_summary(pricing)
    
    tooltip_parts = []
    if model_name:
        tooltip_parts.append(f"<strong>{model_name}</strong><br>")
    
    tooltip_parts.append("💰 Pricing:<br>")
    tooltip_parts.append(f"• Input: {summary['input']}<br>")
    tooltip_parts.append(f"• Output: {summary['output']}<br>")
    
    if "web_search" in summary:
        tooltip_parts.append(f"• Web Search: {summary['web_search']}<br>")
    
    if "request" in summary:
        tooltip_parts.append(f"• Per Request: {summary['request']}<br>")
    
    if "image" in summary:
        tooltip_parts.append(f"• Image: {summary['image']}<br>")
    
    return "".join(tooltip_parts)

