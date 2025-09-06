from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, text, Date # <-- Import Date
from app.database.models import UsageLog, APIKey, User, OllamaServer
import datetime

async def create_usage_log(
    db: AsyncSession, *, api_key_id: int, endpoint: str, status_code: int, server_id: int | None
) -> UsageLog:
    db_log = UsageLog(
        api_key_id=api_key_id, 
        endpoint=endpoint, 
        status_code=status_code,
        server_id=server_id
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
            APIKey.is_revoked,
            func.count(UsageLog.id).label("request_count"),
        )
        .select_from(APIKey)
        .join(User, APIKey.user_id == User.id)
        .outerjoin(UsageLog, APIKey.id == UsageLog.api_key_id)
        .group_by(User.username, APIKey.key_name, APIKey.key_prefix, APIKey.is_revoked)
        .order_by(func.count(UsageLog.id).desc())
    )
    result = await db.execute(stmt)
    return result.all()

# --- NEW STATISTICS FUNCTIONS ---

async def get_daily_usage_stats(db: AsyncSession, days: int = 30):
    """Returns total requests per day for the last N days."""
    start_date = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    
    # --- CRITICAL FIX: Cast the date function output to a Date type ---
    # This ensures that we get a date object back, not just a string,
    # which is required for the strftime formatting in the admin route.
    date_column = func.date(UsageLog.request_timestamp, type_=Date).label("date")
    # --- END FIX ---
    
    stmt = (
        select(
            date_column,
            func.count(UsageLog.id).label("request_count")
        )
        .filter(UsageLog.request_timestamp >= start_date)
        .group_by(date_column)
        .order_by(date_column.asc())
    )
    result = await db.execute(stmt)
    return result.all()

async def get_hourly_usage_stats(db: AsyncSession):
    """Returns total requests aggregated by the hour of the day (UTC)."""
    # This uses strftime which is specific to SQLite.
    # For PostgreSQL, you would use: func.extract('hour', UsageLog.request_timestamp)
    hour_extract = func.strftime('%H', UsageLog.request_timestamp)
    
    stmt = (
        select(
            hour_extract.label("hour"),
            func.count(UsageLog.id).label("request_count")
        )
        .group_by("hour")
        .order_by("hour")
    )
    result = await db.execute(stmt)
    # Ensure all 24 hours are present
    stats_dict = {row.hour: row.request_count for row in result.all()}
    return [{"hour": f"{h:02d}:00", "request_count": stats_dict.get(f"{h:02d}", 0)} for h in range(24)]

async def get_server_load_stats(db: AsyncSession):
    """Returns total requests per backend server."""
    stmt = (
        select(
            OllamaServer.name.label("server_name"),
            func.count(UsageLog.id).label("request_count")
        )
        .select_from(OllamaServer)
        .outerjoin(UsageLog, OllamaServer.id == UsageLog.server_id)
        .group_by(OllamaServer.name)
        .order_by(func.count(UsageLog.id).desc())
    )
    result = await db.execute(stmt)
    return result.all()