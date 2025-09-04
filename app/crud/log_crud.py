from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from app.database.models import UsageLog, APIKey, User


async def create_usage_log(
    db: AsyncSession, *, api_key_id: int, endpoint: str, status_code: int
) -> UsageLog:
    db_log = UsageLog(
        api_key_id=api_key_id, endpoint=endpoint, status_code=status_code
    )
    db.add(db_log)
    await db.commit()
    await db.refresh(db_log)
    return db_log

async def get_usage_statistics(db: AsyncSession):
    """Returns aggregated usage statistics for all API keys."""
    stmt = (
        select(
            User.username,
            APIKey.key_name,
            APIKey.key_prefix,
            func.count(UsageLog.id).label("request_count"),
        )
        .join(APIKey, UsageLog.api_key_id == APIKey.id)
        .join(User, APIKey.user_id == User.id)
        .group_by(User.username, APIKey.key_name, APIKey.key_prefix)
        .order_by(func.count(UsageLog.id).desc())
    )
    result = await db.execute(stmt)
    return result.all()