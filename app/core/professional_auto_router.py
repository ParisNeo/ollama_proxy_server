"""
Professional Auto-Router with Advanced Decision Matrix
Uses capabilities, descriptions, pricing, and priority modes for intelligent model selection.
"""
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from app.database.models import ModelMetadata

logger = logging.getLogger(__name__)


class RoutingRequest:
    """Represents a routing request with all characteristics."""
    def __init__(self, body: Dict[str, Any]):
        self.has_images = bool(body.get("images"))
        self.contains_code = self._detect_code(body)
        self.requires_tool_calling = bool(
            body.get("tools") or 
            body.get("tool_choice") or 
            any(msg.get("tool_calls") for msg in body.get("messages", []) if isinstance(msg, dict))
        )
        self.requires_internet = self._detect_internet_need(body)
        self.requires_thinking = bool(
            body.get("options", {}).get("think") or 
            "think" in self._extract_prompt(body).lower()
        )
        self.requires_fast = bool(body.get("options", {}).get("fast_model"))
        self.prompt_content = self._extract_prompt(body)
        self.prompt_length = len(self.prompt_content)
        self.message_count = len(body.get("messages", []))
    
    def _extract_prompt(self, body: Dict[str, Any]) -> str:
        """Extract prompt content from request."""
        if "prompt" in body:
            return str(body["prompt"])
        elif "messages" in body:
            last_message = body["messages"][-1] if body["messages"] else {}
            if isinstance(last_message.get("content"), str):
                return last_message["content"]
            elif isinstance(last_message.get("content"), list):
                text_part = next(
                    (p.get("text", "") for p in last_message["content"] if p.get("type") == "text"),
                    ""
                )
                return text_part
        return ""
    
    def _detect_code(self, body: Dict[str, Any]) -> bool:
        """Detect if request contains code."""
        prompt = self._extract_prompt(body)
        code_keywords = [
            "def ", "class ", "import ", "const ", "let ", "var ", 
            "function ", "public static void", "int main(", "async def",
            "def ", "return ", "if __name__", "package ", "namespace "
        ]
        return any(kw.lower() in prompt.lower() for kw in code_keywords)
    
    def _detect_internet_need(self, body: Dict[str, Any]) -> bool:
        """Detect if request needs internet/real-time data."""
        prompt = self._extract_prompt(body)
        messages = body.get("messages", [])
        
        # Check prompt for time-sensitive queries
        time_sensitive_keywords = [
            "current", "latest", "now", "today", "recent", "what's happening",
            "price of", "stock", "weather", "news", "breaking", "live"
        ]
        if any(kw in prompt.lower() for kw in time_sensitive_keywords):
            return True
        
        # Check messages for web search context
        for msg in messages:
            msg_str = str(msg).lower()
            if any(kw in msg_str for kw in ["web_search", "internet", "grounding", "real-time", "live data"]):
                return True
        
        return False


class ModelScorer:
    """Scores models based on how well they match a routing request."""
    
    # Capability weights (higher = more important)
    CAPABILITY_WEIGHTS = {
        "images": 10.0,
        "code": 8.0,
        "tool_calling": 9.0,
        "internet": 10.0,
        "thinking": 7.0,
        "fast": 5.0,
    }
    
    # Description keyword weights
    DESCRIPTION_KEYWORDS = {
        "code": {"code", "programming", "coder", "developer", "syntax", "algorithm"},
        "images": {"vision", "image", "multimodal", "visual", "photo", "picture"},
        "thinking": {"reasoning", "think", "reason", "step-by-step", "chain-of-thought"},
        "internet": {"web search", "internet", "grounding", "real-time", "live data"},
        "tool_calling": {"tool", "function", "plugin", "api", "integration"},
        "fast": {"fast", "turbo", "quick", "speed", "low latency", "efficient"},
    }
    
    @staticmethod
    def calculate_match_score(
        metadata: ModelMetadata,
        request: RoutingRequest,
        model_details: Dict[str, Any] = None
    ) -> float:
        """
        Calculate how well a model matches the request.
        Returns a score from 0.0 to 100.0 (higher = better match).
        """
        if model_details is None:
            model_details = {}
        
        score = 0.0
        max_possible_score = 0.0
        
        # 1. Capability matching (exact requirements)
        if request.has_images:
            max_possible_score += ModelScorer.CAPABILITY_WEIGHTS["images"]
            if metadata.supports_images:
                score += ModelScorer.CAPABILITY_WEIGHTS["images"]
            else:
                # Penalty for missing required capability
                score -= ModelScorer.CAPABILITY_WEIGHTS["images"] * 0.5
        
        if request.contains_code:
            max_possible_score += ModelScorer.CAPABILITY_WEIGHTS["code"]
            if metadata.is_code_model:
                score += ModelScorer.CAPABILITY_WEIGHTS["code"]
            else:
                # Check description for code keywords
                if metadata.description:
                    desc_lower = metadata.description.lower()
                    if any(kw in desc_lower for kw in ModelScorer.DESCRIPTION_KEYWORDS["code"]):
                        score += ModelScorer.CAPABILITY_WEIGHTS["code"] * 0.7  # Partial match
        
        if request.requires_tool_calling:
            max_possible_score += ModelScorer.CAPABILITY_WEIGHTS["tool_calling"]
            if metadata.supports_tool_calling:
                score += ModelScorer.CAPABILITY_WEIGHTS["tool_calling"]
            else:
                score -= ModelScorer.CAPABILITY_WEIGHTS["tool_calling"] * 0.5
        
        if request.requires_internet:
            max_possible_score += ModelScorer.CAPABILITY_WEIGHTS["internet"]
            if metadata.supports_internet:
                score += ModelScorer.CAPABILITY_WEIGHTS["internet"]
            else:
                score -= ModelScorer.CAPABILITY_WEIGHTS["internet"] * 0.5
        
        if request.requires_thinking:
            max_possible_score += ModelScorer.CAPABILITY_WEIGHTS["thinking"]
            if metadata.is_thinking_model:
                score += ModelScorer.CAPABILITY_WEIGHTS["thinking"]
            else:
                # Check description
                if metadata.description:
                    desc_lower = metadata.description.lower()
                    if any(kw in desc_lower for kw in ModelScorer.DESCRIPTION_KEYWORDS["thinking"]):
                        score += ModelScorer.CAPABILITY_WEIGHTS["thinking"] * 0.7
        
        if request.requires_fast:
            max_possible_score += ModelScorer.CAPABILITY_WEIGHTS["fast"]
            if metadata.is_fast_model:
                score += ModelScorer.CAPABILITY_WEIGHTS["fast"]
            else:
                # Check description
                if metadata.description:
                    desc_lower = metadata.description.lower()
                    if any(kw in desc_lower for kw in ModelScorer.DESCRIPTION_KEYWORDS["fast"]):
                        score += ModelScorer.CAPABILITY_WEIGHTS["fast"] * 0.7
        
        # 2. Description analysis (bonus points for relevant keywords)
        if metadata.description:
            desc_lower = metadata.description.lower()
            description_bonus = 0.0
            
            # Check for relevant keywords in description
            if request.contains_code:
                if any(kw in desc_lower for kw in ModelScorer.DESCRIPTION_KEYWORDS["code"]):
                    description_bonus += 3.0
            
            if request.has_images:
                if any(kw in desc_lower for kw in ModelScorer.DESCRIPTION_KEYWORDS["images"]):
                    description_bonus += 3.0
            
            if request.requires_internet:
                if any(kw in desc_lower for kw in ModelScorer.DESCRIPTION_KEYWORDS["internet"]):
                    description_bonus += 3.0
            
            score += description_bonus
            max_possible_score += 9.0  # Max description bonus
        
        # 3. Context length matching (bonus for long context if needed)
        context_length = model_details.get("context_length", 0)
        if request.prompt_length > 10000 or request.message_count > 20:
            # Long conversation - prefer models with large context
            if context_length >= 200000:
                score += 5.0
            elif context_length >= 100000:
                score += 3.0
            elif context_length >= 50000:
                score += 1.0
            max_possible_score += 5.0
        
        # 4. Capability diversity bonus (models with multiple relevant capabilities)
        relevant_capabilities = 0
        if request.has_images and metadata.supports_images:
            relevant_capabilities += 1
        if request.contains_code and metadata.is_code_model:
            relevant_capabilities += 1
        if request.requires_tool_calling and metadata.supports_tool_calling:
            relevant_capabilities += 1
        if request.requires_internet and metadata.supports_internet:
            relevant_capabilities += 1
        if request.requires_thinking and metadata.is_thinking_model:
            relevant_capabilities += 1
        
        if relevant_capabilities >= 2:
            score += 5.0  # Bonus for multi-capability models
        max_possible_score += 5.0
        
        # Normalize score to 0-100 range
        if max_possible_score > 0:
            normalized_score = (score / max_possible_score) * 100.0
        else:
            # No specific requirements - give base score
            normalized_score = 50.0
        
        # Ensure score is in valid range
        normalized_score = max(0.0, min(100.0, normalized_score))
        
        return normalized_score


class ProfessionalAutoRouter:
    """Professional auto-router with decision matrix and priority mode support."""
    
    @staticmethod
    async def select_best_model(
        available_metadata: List[ModelMetadata],
        request: RoutingRequest,
        model_details_map: Dict[str, Dict[str, Any]] = None,
        priority_mode: str = "free"  # "free", "daily_drive", "advanced", "luxury"
    ) -> Optional[Tuple[ModelMetadata, float]]:
        """
        Select the best model using professional routing algorithm.
        
        Returns:
            Tuple of (best_model, match_score) or None if no models available
        """
        if not available_metadata:
            return None
        
        if model_details_map is None:
            model_details_map = {}
        
        # Step 1: Filter by priority level (respect priority mode)
        priority_filtered = ProfessionalAutoRouter._filter_by_priority_mode(
            available_metadata, priority_mode, model_details_map
        )
        
        if not priority_filtered:
            logger.warning(f"No models available for priority mode '{priority_mode}'. Falling back to all models.")
            priority_filtered = available_metadata
        
        # Step 2: Score each model
        scored_models = []
        for metadata in priority_filtered:
            model_details = model_details_map.get(metadata.model_name, {})
            match_score = ModelScorer.calculate_match_score(metadata, request, model_details)
            
            # Adjust score based on priority (lower priority number = higher priority)
            priority_bonus = (11 - metadata.priority) * 2.0  # Max 20 point bonus for priority 1
            final_score = match_score + priority_bonus
            
            scored_models.append((metadata, final_score, match_score))
        
        # Step 3: Sort by final score (descending)
        scored_models.sort(key=lambda x: x[1], reverse=True)
        
        # Step 4: Select best model
        if scored_models:
            best_metadata, final_score, match_score = scored_models[0]
            
            logger.info(
                f"Auto-router selected '{best_metadata.model_name}' "
                f"(priority: {best_metadata.priority}, match: {match_score:.1f}%, final: {final_score:.1f})"
            )
            
            # Log top 3 candidates for debugging
            if len(scored_models) > 1:
                top_3 = scored_models[:3]
                logger.debug("Top 3 candidates:")
                for i, (meta, final, match) in enumerate(top_3, 1):
                    logger.debug(
                        f"  {i}. {meta.model_name} "
                        f"(priority: {meta.priority}, match: {match:.1f}%, final: {final:.1f})"
                    )
            
            return (best_metadata, match_score)
        
        return None
    
    @staticmethod
    def _filter_by_priority_mode(
        metadata_list: List[ModelMetadata],
        priority_mode: str,
        model_details_map: Dict[str, Dict[str, Any]]
    ) -> List[ModelMetadata]:
        """
        Filter models based on priority mode.
        Returns models that should be considered for this mode.
        """
        from app.core.priority_modes import (
            is_free_model,
            is_ollama_cloud_model,
            is_top_tier_model,
            is_mid_tier_model
        )
        
        if priority_mode == "free":
            # Free mode: Only free models
            return [m for m in metadata_list if is_free_model(m, model_details_map)]
        
        elif priority_mode == "daily_drive":
            # Daily Drive: Ollama cloud models first, then free
            cloud_models = [m for m in metadata_list if is_ollama_cloud_model(m.model_name)]
            free_models = [m for m in metadata_list if is_free_model(m, model_details_map) and m not in cloud_models]
            return cloud_models + free_models
        
        elif priority_mode == "advanced":
            # Advanced: Top-tier and mid-tier paid models only
            return [
                m for m in metadata_list
                if (is_top_tier_model(m.model_name, m) or 
                    is_mid_tier_model(m.model_name, m, model_details_map)) and
                   not is_free_model(m, model_details_map)
            ]
        
        elif priority_mode == "luxury":
            # Luxury: Only Opus-level and similarly priced models
            # Models priced $6-15 per 1M input tokens (Opus-level)
            luxury_models = []
            for m in metadata_list:
                if is_free_model(m, model_details_map):
                    continue
                
                # Check pricing
                details = model_details_map.get(m.model_name, {})
                pricing = details.get("pricing", {})
                prompt_price = pricing.get("prompt", 0)
                
                try:
                    prompt_price = float(prompt_price) if prompt_price else 0
                    # Opus-level: $6-15 per 1M tokens
                    if 6.0 <= prompt_price <= 15.0:
                        luxury_models.append(m)
                except (ValueError, TypeError):
                    # Check by name patterns if pricing unavailable
                    model_name_lower = m.model_name.lower()
                    if any(p in model_name_lower for p in ["opus", "claude-opus", "gpt-4"]):
                        luxury_models.append(m)
            
            return luxury_models
        
        else:
            # Unknown mode - return all
            logger.warning(f"Unknown priority mode '{priority_mode}', using all models")
            return metadata_list

