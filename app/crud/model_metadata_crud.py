"""Model metadata CRUD operations for Ollama Proxy Server."""

from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database.models import ModelMetadata


async def get_metadata_by_model_name(db: AsyncSession, model_name: str) -> Optional[ModelMetadata]:
    """Get metadata by model name from database."""
    result = await db.execute(select(ModelMetadata).filter(ModelMetadata.model_name == model_name))
    return result.scalars().first()


async def get_or_create_metadata(db: AsyncSession, model_name: str) -> ModelMetadata:
    """Get or create metadata for a model with default entry if missing."""
    metadata = await get_metadata_by_model_name(db, model_name)
    if not metadata:
        # Basic heuristic for multi-modal models
        supports_images_default = "llava" in model_name or "bakllava" in model_name

        metadata = ModelMetadata(model_name=model_name, supports_images=supports_images_default, description="Auto-discovered model.")
        db.add(metadata)
        await db.commit()
        await db.refresh(metadata)
    return metadata


async def get_all_metadata(db: AsyncSession) -> List[ModelMetadata]:
    """Get all model metadata records sorted by priority then name."""
    result = await db.execute(select(ModelMetadata).order_by(ModelMetadata.priority, ModelMetadata.model_name))
    return result.scalars().all()


async def update_metadata(db: AsyncSession, model_name: str, **kwargs) -> Optional[ModelMetadata]:
    """Update metadata for a specific model."""
    metadata = await get_metadata_by_model_name(db, model_name)
    if not metadata:
        return None

    for key, value in kwargs.items():
        if hasattr(metadata, key):
            setattr(metadata, key, value)

    await db.commit()
    await db.refresh(metadata)
    return metadata
