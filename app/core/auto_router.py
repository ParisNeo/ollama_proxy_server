"""
Professional Auto-Routing System
Uses model capabilities, descriptions, priority modes, and intelligent scoring
to select the best model for each request.
"""
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from app.database.models import ModelMetadata

logger = logging.getLogger(__name__)


class AutoRouter:
    """
    Professional auto-routing system that intelligently selects models based on:
    - Request characteristics (images, code, tool calling, internet, thinking, fast)
    - Model capabilities (from metadata)
    - Model descriptions (semantic matching)
    - Priority modes (Free, Daily Drive, Advanced, Luxury)
    - Budget considerations
    """
    
    def __init__(self, priority_mode: str = "free"):
        """
        Initialize auto-router with a priority mode.
        
        Args:
            priority_mode: "free", "daily_drive", "advanced", or "luxury"
        """
        self.priority_mode = priority_mode.lower()
        if self.priority_mode not in ["free", "daily_drive", "advanced", "luxury"]:
            self.priority_mode = "free"
            logger.warning(f"Unknown priority mode, defaulting to 'free'")
    
    def analyze_request(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze the request to determine what capabilities are needed.
        
        Returns a dict with:
        - has_images: bool
        - contains_code: bool
        - requires_tool_calling: bool
        - requires_internet: bool
        - requires_thinking: bool
        - requires_fast: bool
        - request_type: str (e.g., "code", "multimodal", "reasoning", "general")
        - keywords: List[str] (extracted from prompt for semantic matching)
        """
        analysis = {
            "has_images": False,
            "contains_code": False,
            "requires_tool_calling": False,
            "requires_internet": False,
            "requires_thinking": False,
            "requires_fast": False,
            "request_type": "general",
            "keywords": []
        }
        
        # Extract prompt content
        prompt_content = ""
        if "prompt" in body:  # generate endpoint
            prompt_content = body["prompt"]
        elif "messages" in body:  # chat endpoint
            last_message = body["messages"][-1] if body["messages"] else {}
            if isinstance(last_message.get("content"), str):
                prompt_content = last_message["content"]
            elif isinstance(last_message.get("content"), list):  # multimodal chat
                text_part = next((p.get("text", "") for p in last_message["content"] if p.get("type") == "text"), "")
                prompt_content = text_part
                # Check for images in multimodal content
                if any(p.get("type") == "image" for p in last_message.get("content", [])):
                    analysis["has_images"] = True
        
        # Check for images in body
        if "images" in body and body["images"]:
            analysis["has_images"] = True
        
        # Check for code
        code_keywords = [
            "def ", "class ", "import ", "const ", "let ", "var ", "function ",
            "public static void", "int main(", "async def", "export ", "module ",
            "package ", "interface ", "struct ", "enum ", "typedef"
        ]
        analysis["contains_code"] = any(kw.lower() in prompt_content.lower() for kw in code_keywords)
        
        # Check for tool calling
        analysis["requires_tool_calling"] = (
            body.get("tools") is not None or
            body.get("tool_choice") is not None or
            any(msg.get("tool_calls") for msg in body.get("messages", []) if isinstance(msg, dict))
        )
        
        # Check for internet/grounding
        analysis["requires_internet"] = any(
            "web_search" in str(msg).lower() or 
            "internet" in str(msg).lower() or 
            "grounding" in str(msg).lower() or
            "real-time" in str(msg).lower() or
            "current" in str(msg).lower() and ("news" in str(msg).lower() or "today" in str(msg).lower() or "now" in str(msg).lower())
            for msg in body.get("messages", [])
        )
        
        # Check for thinking/reasoning
        analysis["requires_thinking"] = (
            body.get("options", {}).get("think") is not None or
            "think" in prompt_content.lower() or
            "reasoning" in prompt_content.lower() or
            "step by step" in prompt_content.lower() or
            "chain of thought" in prompt_content.lower()
        )
        
        # Check for fast model request
        analysis["requires_fast"] = body.get("options", {}).get("fast_model", False)
        
        # Determine request type
        if analysis["has_images"]:
            if analysis["contains_code"]:
                analysis["request_type"] = "multimodal_code"
            else:
                analysis["request_type"] = "multimodal"
        elif analysis["contains_code"]:
            analysis["request_type"] = "code"
        elif analysis["requires_thinking"]:
            analysis["request_type"] = "reasoning"
        elif analysis["requires_tool_calling"]:
            analysis["request_type"] = "tool_use"
        elif analysis["requires_internet"]:
            analysis["request_type"] = "web_search"
        else:
            analysis["request_type"] = "general"
        
        # Extract keywords from prompt for semantic matching
        # Remove common words and extract meaningful terms
        stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by", "is", "are", "was", "were", "be", "been", "have", "has", "had", "do", "does", "did", "will", "would", "could", "should", "may", "might", "can", "this", "that", "these", "those", "i", "you", "he", "she", "it", "we", "they", "what", "which", "who", "where", "when", "why", "how"}
        words = re.findall(r'\b[a-z]{3,}\b', prompt_content.lower())
        analysis["keywords"] = [w for w in words if w not in stop_words][:20]  # Top 20 keywords
        
        return analysis
    
    def score_model(self, metadata: ModelMetadata, request_analysis: Dict[str, Any], model_details: Dict[str, Any] = None) -> float:
        """
        Score a model based on how well it matches the request.
        
        Returns a score from 0.0 to 100.0, where higher is better.
        
        Scoring factors:
        1. Capability matches (required capabilities must be present)
        2. Description semantic matching (keywords in description)
        3. Priority level (lower priority = higher base score)
        4. Capability completeness (more matching capabilities = higher score)
        5. Budget considerations (for luxury mode)
        """
        score = 0.0
        max_score = 100.0
        
        # Base score from priority (lower priority number = higher base score)
        # Priority 1 gets 50 points, Priority 2 gets 40, Priority 3 gets 30, etc.
        if metadata.priority is not None:
            base_score = max(0, 60 - (metadata.priority * 10))
            score += base_score
        else:
            score += 20  # Low score for unprioritized models
        
        # Capability matching (required capabilities must be present)
        capability_penalty = 0
        capability_bonus = 0
        
        if request_analysis["has_images"]:
            if metadata.supports_images:
                capability_bonus += 10
            else:
                capability_penalty += 50  # Heavy penalty for missing required capability
        
        if request_analysis["contains_code"]:
            if metadata.is_code_model:
                capability_bonus += 10
            else:
                capability_penalty += 30  # Penalty for missing code capability
        
        if request_analysis["requires_tool_calling"]:
            if metadata.supports_tool_calling:
                capability_bonus += 10
            else:
                capability_penalty += 50  # Heavy penalty for missing required capability
        
        if request_analysis["requires_internet"]:
            if metadata.supports_internet:
                capability_bonus += 10
            else:
                capability_penalty += 50  # Heavy penalty for missing required capability
        
        if request_analysis["requires_thinking"]:
            if metadata.is_thinking_model:
                capability_bonus += 10
            else:
                capability_penalty += 30  # Penalty for missing thinking capability
        
        if request_analysis["requires_fast"]:
            if metadata.is_fast_model:
                capability_bonus += 5
            else:
                capability_penalty += 20  # Moderate penalty for missing fast capability
        
        score += capability_bonus
        score -= capability_penalty
        
        # Description semantic matching (if description exists)
        if metadata.description and request_analysis["keywords"]:
            description_lower = metadata.description.lower()
            matching_keywords = sum(1 for keyword in request_analysis["keywords"] if keyword in description_lower)
            if matching_keywords > 0:
                # Bonus for keyword matches (up to 15 points)
                keyword_score = min(15, (matching_keywords / len(request_analysis["keywords"])) * 15)
                score += keyword_score
        
        # Capability completeness bonus (models with more capabilities get slight bonus)
        capability_count = sum([
            metadata.supports_images,
            metadata.is_code_model,
            metadata.supports_tool_calling,
            metadata.supports_internet,
            metadata.is_thinking_model,
            metadata.is_fast_model
        ])
        if capability_count >= 3:
            score += 5  # Bonus for versatile models
        
        # Budget considerations for Luxury mode
        if self.priority_mode == "luxury":
            # In luxury mode, prefer higher-priced models (they're typically better)
            if model_details:
                pricing = model_details.get("pricing", {})
                prompt_price = pricing.get("prompt", 0)
                try:
                    prompt_price = float(prompt_price) if prompt_price else 0
                    # Bonus for expensive models (up to 10 points)
                    if prompt_price > 5.0:  # Top-tier pricing
                        score += 10
                    elif prompt_price > 1.0:  # Mid-tier pricing
                        score += 5
                except (ValueError, TypeError):
                    pass
        
        # Ensure score is within bounds
        score = max(0.0, min(max_score, score))
        
        return score
    
    def select_best_model(
        self,
        available_models: List[ModelMetadata],
        request_analysis: Dict[str, Any],
        model_details_map: Dict[str, Dict[str, Any]] = None
    ) -> Optional[Tuple[ModelMetadata, float]]:
        """
        Select the best model from available models based on scoring.
        
        Returns:
            Tuple of (ModelMetadata, score) or None if no suitable model found
        """
        if not available_models:
            return None
        
        if model_details_map is None:
            model_details_map = {}
        
        # Score all models
        scored_models = []
        for model in available_models:
            details = model_details_map.get(model.model_name, {})
            score = self.score_model(model, request_analysis, details)
            
            # Skip models with negative scores (missing required capabilities)
            if score >= 0:
                scored_models.append((model, score))
        
        if not scored_models:
            logger.warning("Auto-routing: No models scored >= 0. All models missing required capabilities.")
            return None
        
        # Sort by score (descending), then by priority (ascending) as tiebreaker
        scored_models.sort(key=lambda x: (-x[1], x[0].priority or 999))
        
        best_model, best_score = scored_models[0]
        
        logger.info(
            f"Auto-routing ({self.priority_mode}): Selected '{best_model.model_name}' "
            f"with score {best_score:.2f}/100.0 (priority {best_model.priority})"
        )
        
        if len(scored_models) > 1:
            logger.debug(f"Top 3 candidates: {[(m.model_name, f'{s:.2f}') for m, s in scored_models[:3]]}")
        
        return (best_model, best_score)

