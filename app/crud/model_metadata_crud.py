from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from typing import List, Optional

from app.database.models import ModelMetadata

async def get_metadata_by_model_name(db: AsyncSession, model_name: str) -> Optional[ModelMetadata]:
    result = await db.execute(select(ModelMetadata).filter(ModelMetadata.model_name == model_name))
    return result.scalars().first()

async def get_or_create_metadata(db: AsyncSession, model_name: str) -> ModelMetadata:
    """Gets metadata for a model, creating a default entry if it doesn't exist."""
    metadata = await get_metadata_by_model_name(db, model_name)
    if not metadata:
        # Basic heuristic for multi-modal models
        model_name_lower = model_name.lower()
        supports_images_default = (
            "llava" in model_name_lower or 
            "bakllava" in model_name_lower or
            "vision" in model_name_lower or
            "gpt-4" in model_name_lower or  # GPT-4 models support images
            "claude-3" in model_name_lower or  # Claude 3 models support images
            "gemini" in model_name_lower  # Gemini models support images
        )
        
        # Heuristic for code models
        is_code_model_default = (
            "code" in model_name_lower or
            "codellama" in model_name_lower or
            "deepseek-coder" in model_name_lower or
            "starcoder" in model_name_lower
        )
        
        # Heuristic for fast models (smaller/faster models)
        is_fast_model_default = (
            "7b" in model_name_lower or
            "8b" in model_name_lower or
            "3b" in model_name_lower or
            "1b" in model_name_lower or
            "turbo" in model_name_lower or
            "fast" in model_name_lower
        )
        
        # Heuristic for tool calling (models that support function calling)
        supports_tool_calling_default = (
            "gpt-4" in model_name_lower or
            "gpt-3.5" in model_name_lower or
            "claude-3" in model_name_lower or
            "claude-4" in model_name_lower or
            "gemini" in model_name_lower or
            "o1" in model_name_lower or
            "tool" in model_name_lower
        )
        
        # Heuristic for internet/grounding (models with web search capabilities)
        # Only mark models that are EXPLICITLY designed for internet/web search
        # Do NOT assume flagship models have internet - many run locally without web access
        supports_internet_default = (
            "grok" in model_name_lower or
            "perplexity" in model_name_lower or
            "you.com" in model_name_lower or
            "phind" in model_name_lower or
            "brave" in model_name_lower or
            "search" in model_name_lower or
            "grounding" in model_name_lower or
            "web-browsing" in model_name_lower or
            "internet-access" in model_name_lower
        )
        
        # Heuristic for thinking models (chain-of-thought reasoning)
        is_thinking_model_default = (
            ("thinking" in model_name_lower or "think" in model_name_lower) and
            "nothink" not in model_name_lower
        ) or (
            "o1" in model_name_lower or
            "o3" in model_name_lower or
            "reasoning" in model_name_lower or
            "cot" in model_name_lower
        )
        
        # OpenRouter models often have provider prefix (e.g., "openai/gpt-4o")
        description = "Auto-discovered model."
        if "/" in model_name:
            provider = model_name.split("/")[0]
            description = f"Auto-discovered OpenRouter model ({provider})."
        
        metadata = ModelMetadata(
            model_name=model_name,
            supports_images=supports_images_default,
            is_code_model=is_code_model_default,
            is_fast_model=is_fast_model_default,
            supports_tool_calling=supports_tool_calling_default,
            supports_internet=supports_internet_default,
            is_thinking_model=is_thinking_model_default,
            description=description
        )
        db.add(metadata)
        await db.commit()
        await db.refresh(metadata)
    return metadata

async def get_all_metadata(db: AsyncSession) -> List[ModelMetadata]:
    """Gets all model metadata records, sorted by priority then name."""
    result = await db.execute(select(ModelMetadata).order_by(ModelMetadata.priority, ModelMetadata.model_name))
    return result.scalars().all()

async def update_metadata(db: AsyncSession, model_name: str, **kwargs) -> Optional[ModelMetadata]:
    """Updates metadata for a specific model."""
    metadata = await get_metadata_by_model_name(db, model_name)
    if not metadata:
        return None
        
    for key, value in kwargs.items():
        if hasattr(metadata, key):
            setattr(metadata, key, value)
            
    await db.commit()
    await db.refresh(metadata)
    return metadata
