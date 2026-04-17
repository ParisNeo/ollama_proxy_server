from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from typing import List, Optional

from app.database.models import ModelMetadata

async def get_metadata_by_model_name(db: AsyncSession, model_name: str) -> Optional[ModelMetadata]:
    result = await db.execute(select(ModelMetadata).filter(ModelMetadata.model_name == model_name))
    return result.scalars().first()

MODEL_SIZE_LUT = {
    "llama3": 8.0, "llama3.1": 8.0, "llama3.2": 3.0, "phi3": 3.8, "phi4": 14.0,
    "mistral": 7.3, "mixtral": 46.7, "gemma": 7.0, "gemma2": 9.0, "qwen2.5": 7.0,
    "deepseek-v3": 671.0, "deepseek-r1": 671.0
}

MODEL_CONTEXT_LUT = {
    "llama3": 8192, "llama3.1": 128000, "llama3.2": 128000, "phi3": 4096, "phi4": 16384,
    "mistral": 32768, "mixtral": 32768, "gemma": 8192, "gemma2": 8192, "qwen2.5": 128000,
    "deepseek-v3": 128000, "deepseek-r1": 128000, "command-r": 128000
}

def _estimate_max_context(name: str) -> int:
    """Heuristic to extract context window from name or LUT."""
    name_lower = name.lower()
    # 1. Look for patterns like '128k', '32k', '1m'
    match = re.search(r'-(\d+)([km])', name_lower)
    if match:
        val = int(match.group(1))
        unit = match.group(2)
        return val * 1000 if unit == 'k' else val * 1000000
    
    # 2. Check LUT
    base_name = name_lower.split(':')[0].split('-')[0]
    return MODEL_CONTEXT_LUT.get(base_name, 8192) # Default Ollama context

def _estimate_model_size(name: str) -> float:
    """Heuristic to extract parameter count from name or LUT."""
    name_lower = name.lower()
    # 1. Look for explicit '8b', '70b', '1.5b' patterns
    match = re.search(r'(\d+(\.\d+)?)b', name_lower)
    if match:
        return float(match.group(1))
    
    # 2. Check LUT for base names
    base_name = name_lower.split(':')[0].split('-')[0]
    return MODEL_SIZE_LUT.get(base_name, -1.0)

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
            max_context=suggested_ctx if suggested_ctx else _estimate_max_context(model_name),
            model_size=_estimate_model_size(model_name),
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
