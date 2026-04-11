from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from typing import List, Optional

from app.database.models import ModelMetadata

async def get_metadata_by_model_name(db: AsyncSession, model_name: str) -> Optional[ModelMetadata]:
    result = await db.execute(select(ModelMetadata).filter(ModelMetadata.model_name == model_name))
    return result.scalars().first()

async def get_or_create_metadata(db: AsyncSession, model_name: str, suggested_ctx: int = None) -> ModelMetadata:
    """Gets metadata for a model, creating a default entry if it doesn't exist."""
    metadata = await get_metadata_by_model_name(db, model_name)
    if not metadata:
        # Basic heuristic for auto-discovery
        m_lower = model_name.lower()
        # Explicitly define as embedding if name contains keywords
        is_embedding_default = any(kw in m_lower for kw in ["embed", "embedding", "bge", "gte", "nomic", "snowflake"])
        
        # If it is an embedding model, it should NOT support chat, images or thinking by default
        supports_images_default = not is_embedding_default and any(kw in m_lower for kw in ["llava", "bakllava", "vision", "vl"])
        supports_thinking_default = not is_embedding_default and any(kw in m_lower for kw in ["qwen", "deepseek", "r1", "thought", "think", "gemma3", "phi-4"])
        
        metadata = ModelMetadata(
            model_name=model_name,
            supports_images=supports_images_default,
            supports_thinking=supports_thinking_default,
            is_embedding_model=is_embedding_default,
            is_chat_model=not is_embedding_default,
            max_context=suggested_ctx if suggested_ctx else 8192,
            description="Auto-discovered model."
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
