from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func
from app.database.models import User, APIKey, UsageLog
from app.schema.user import UserCreate
from app.core.security import get_password_hash
from typing import Optional


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).filter(User.username == username))
    return result.scalars().first()


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(select(User).filter(User.id == user_id))
    return result.scalars().first()


async def get_users(db: AsyncSession, skip: int = 0, limit: int = 100) -> list:
    """
    Retrieves a list of users along with their statistics:
    - key_count: The number of API keys associated with the user.
    - request_count: The total number of requests made with the user's keys.
    - last_used: The timestamp of the most recent request.
    """
    # Subquery to find the last usage time for each user
    last_used_subq = (
        select(
            APIKey.user_id,
            func.max(UsageLog.request_timestamp).label("last_used")
        )
        .join(UsageLog, APIKey.id == UsageLog.api_key_id)
        .group_by(APIKey.user_id)
        .subquery()
    )

    # Main query to aggregate stats for each user
    stmt = (
        select(
            User.id,
            User.username,
            User.is_admin,
            func.count(func.distinct(APIKey.id)).label("key_count"),
            func.count(UsageLog.id).label("request_count"),
            last_used_subq.c.last_used
        )
        .outerjoin(APIKey, User.id == APIKey.user_id)
        .outerjoin(UsageLog, APIKey.id == UsageLog.api_key_id)
        .outerjoin(last_used_subq, User.id == last_used_subq.c.user_id)
        .group_by(
            User.id,
            User.username,
            User.is_admin,
            last_used_subq.c.last_used
        )
        .order_by(User.username)
        .offset(skip)
        .limit(limit)
    )

    result = await db.execute(stmt)
    return result.all()


async def create_user(db: AsyncSession, user: UserCreate, is_admin: bool = False) -> User:
    hashed_password = get_password_hash(user.password)
    db_user = User(
        username=user.username,
        hashed_password=hashed_password,
        is_admin=is_admin,
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user


async def update_user(db: AsyncSession, user_id: int, username: str, password: Optional[str] = None) -> User | None:
    """Updates a user's username and optionally their password."""
    user = await get_user_by_id(db, user_id=user_id)
    if not user:
        return None

    user.username = username
    if password:
        user.hashed_password = get_password_hash(password)
    
    await db.commit()
    await db.refresh(user)
    return user


async def delete_user(db: AsyncSession, user_id: int) -> User | None:
    user = await get_user_by_id(db, user_id=user_id)
    if user:
        await db.delete(user)
        await db.commit()
    return user