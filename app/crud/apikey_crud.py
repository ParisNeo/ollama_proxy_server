import secrets
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update

from app.database.models import APIKey
from app.core.security import get_api_key_hash


async def get_api_key_by_prefix(db: AsyncSession, prefix: str) -> APIKey | None:
    result = await db.execute(select(APIKey).filter(APIKey.key_prefix == prefix))
    return result.scalars().first()


async def get_api_key_by_id(db: AsyncSession, key_id: int) -> APIKey | None:
    result = await db.execute(select(APIKey).filter(APIKey.id == key_id))
    return result.scalars().first()


async def get_api_keys_for_user(db: AsyncSession, user_id: int) -> list[APIKey]:
    result = await db.execute(select(APIKey).filter(APIKey.user_id == user_id).order_by(APIKey.created_at.desc()))
    return result.scalars().all()


async def create_api_key(
    db: AsyncSession, user_id: int, key_name: str
) -> (str, APIKey):
    """
    Generates a new API key, stores its hash, and returns the plain key and the DB object.
    The plain key is only available at creation time.
    """
    prefix = f"op_{secrets.token_urlsafe(8)}"
    secret = secrets.token_urlsafe(32)
    plain_key = f"{prefix}_{secret}"

    hashed_key = get_api_key_hash(secret)

    db_api_key = APIKey(
        key_name=key_name,
        hashed_key=hashed_key,
        key_prefix=prefix,
        user_id=user_id,
    )
    db.add(db_api_key)
    await db.commit()
    await db.refresh(db_api_key)
    return plain_key, db_api_key


async def revoke_api_key(db: AsyncSession, key_id: int) -> APIKey | None:
    stmt = (
        update(APIKey)
        .where(APIKey.id == key_id)
        .values(is_revoked=True)
        .returning(APIKey)
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.scalars().first()