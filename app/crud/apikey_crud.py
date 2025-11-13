# app/crud/apikey_crud.py
"""API key CRUD operations for Ollama Proxy Server."""

import secrets
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.security import get_api_key_hash
from app.database.models import APIKey


async def get_api_key_by_prefix(db: AsyncSession, prefix: str) -> Optional[APIKey]:
    """Get API key by prefix from database."""
    result = await db.execute(select(APIKey).filter(APIKey.key_prefix == prefix))
    return result.scalars().first()


async def get_api_key_by_id(db: AsyncSession, key_id: int) -> APIKey | None:
    """Get API key by ID from database."""
    result = await db.execute(select(APIKey).filter(APIKey.id == key_id))
    return result.scalars().first()


async def get_api_keys_for_user(db: AsyncSession, user_id: int) -> list[APIKey]:
    """Get all API keys for a user from database."""
    result = await db.execute(select(APIKey).filter(APIKey.user_id == user_id).order_by(APIKey.created_at.desc()))
    return list(result.scalars().all())


async def create_api_key(
    db: AsyncSession, user_id: int, key_name: str, rate_limit_requests: Optional[int] = None, rate_limit_window_minutes: Optional[int] = None
) -> tuple[str, APIKey]:
    """Generate a new API key, store its hash, and return the plain key and the DB object.

    The plain key is only available at creation time.
    """
    # --- CRITICAL FIX: Use token_hex to guarantee no underscores in random parts ---
    # This makes the '_' a reliable delimiter.
    prefix_random_part = secrets.token_hex(8)
    prefix = f"op_{prefix_random_part}"

    secret = secrets.token_hex(24)
    plain_key = f"{prefix}_{secret}"
    # --- END FIX ---

    hashed_key = get_api_key_hash(secret)

    db_api_key = APIKey(
        key_name=key_name, hashed_key=hashed_key, key_prefix=prefix, user_id=user_id, rate_limit_requests=rate_limit_requests, rate_limit_window_minutes=rate_limit_window_minutes
    )
    db.add(db_api_key)
    await db.commit()
    await db.refresh(db_api_key)
    return plain_key, db_api_key


async def revoke_api_key(db: AsyncSession, key_id: int) -> APIKey | None:
    """Revoke an API key by ID."""
    stmt = update(APIKey).where(APIKey.id == key_id).values(is_revoked=True, is_active=False).returning(APIKey)  # Revoking also deactivates
    result = await db.execute(stmt)
    await db.commit()
    return result.scalars().first()


async def toggle_api_key_active(db: AsyncSession, key_id: int) -> APIKey | None:
    """Toggles the is_active status of an API key."""
    key = await get_api_key_by_id(db, key_id)
    if not key or key.is_revoked:
        return None

    key.is_active = not key.is_active
    await db.commit()
    await db.refresh(key)
    return key


async def get_api_key_by_name_and_user_id(db: AsyncSession, *, key_name: str, user_id: int) -> APIKey | None:
    """Get an API key by its name for a specific user."""
    stmt = select(APIKey).filter(APIKey.user_id == user_id, APIKey.key_name == key_name)
    result = await db.execute(stmt)
    return result.scalars().first()
