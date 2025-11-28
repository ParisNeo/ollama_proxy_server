# app/crud/log_crud.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, text, Date # <-- Import Date
from app.database.models import UsageLog, APIKey, User, OllamaServer
import datetime

async def create_usage_log(
    db: AsyncSession, *, api_key_id: int, endpoint: str, status_code: int, server_id: int | None, model: str | None = None
) -> UsageLog:
    db_log = UsageLog(
        api_key_id=api_key_id,
        endpoint=endpoint,
        status_code=status_code,
        server_id=server_id,
        model=model
    )
    db.add(db_log)
    await db.commit()
    await db.refresh(db_log)
    return db_log

async def get_usage_statistics(db: AsyncSession, sort_by: str = "request_count", sort_order: str = "desc"):
    """
    Returns aggregated usage statistics for all API keys, with sorting.
    """
    sort_column_map = {
        "username": User.username,
        "key_name": APIKey.key_name,
        "key_prefix": APIKey.key_prefix,
        "request_count": func.count(UsageLog.id),
    }

    # Default to request_count if an invalid column is provided for safety
    sort_column = sort_column_map.get(sort_by, func.count(UsageLog.id))

    # Determine sort order
    if sort_order.lower() == "asc":
        order_modifier = sort_column.asc()
    else:
        order_modifier = sort_column.desc()

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
        .order_by(order_modifier)
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

async def get_model_usage_stats(db: AsyncSession):
    """Returns total requests per model."""
    stmt = (
        select(
            UsageLog.model.label("model_name"),
            func.count(UsageLog.id).label("request_count")
        )
        .filter(UsageLog.model.isnot(None))
        .group_by(UsageLog.model)
        .order_by(func.count(UsageLog.id).desc())
    )
    result = await db.execute(stmt)
    return result.all()

# --- NEW USER-SPECIFIC STATISTICS FUNCTIONS ---

async def get_daily_usage_stats_for_user(db: AsyncSession, user_id: int, days: int = 30):
    """Returns total requests per day for the last N days for a specific user."""
    start_date = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    date_column = func.date(UsageLog.request_timestamp, type_=Date).label("date")
    
    stmt = (
        select(
            date_column,
            func.count(UsageLog.id).label("request_count")
        )
        .join(APIKey, UsageLog.api_key_id == APIKey.id)
        .filter(APIKey.user_id == user_id)
        .filter(UsageLog.request_timestamp >= start_date)
        .group_by(date_column)
        .order_by(date_column.asc())
    )
    result = await db.execute(stmt)
    return result.all()

async def get_hourly_usage_stats_for_user(db: AsyncSession, user_id: int):
    """Returns total requests aggregated by the hour for a specific user."""
    hour_extract = func.strftime('%H', UsageLog.request_timestamp)
    
    stmt = (
        select(
            hour_extract.label("hour"),
            func.count(UsageLog.id).label("request_count")
        )
        .join(APIKey, UsageLog.api_key_id == APIKey.id)
        .filter(APIKey.user_id == user_id)
        .group_by("hour")
        .order_by("hour")
    )
    result = await db.execute(stmt)
    stats_dict = {row.hour: row.request_count for row in result.all()}
    return [{"hour": f"{h:02d}:00", "request_count": stats_dict.get(f"{h:02d}", 0)} for h in range(24)]

async def get_server_load_stats_for_user(db: AsyncSession, user_id: int):
    """Returns total requests per backend server for a specific user."""
    stmt = (
        select(
            OllamaServer.name.label("server_name"),
            func.count(UsageLog.id).label("request_count")
        )
        .select_from(UsageLog)
        .join(APIKey, UsageLog.api_key_id == APIKey.id)
        .outerjoin(OllamaServer, UsageLog.server_id == OllamaServer.id)
        .filter(APIKey.user_id == user_id)
        .group_by(OllamaServer.name)
        .order_by(func.count(UsageLog.id).desc())
    )
    result = await db.execute(stmt)
    return result.all()

async def get_model_usage_stats_for_user(db: AsyncSession, user_id: int):
    """Returns total requests per model for a specific user."""
    stmt = (
        select(
            UsageLog.model.label("model_name"),
            func.count(UsageLog.id).label("request_count")
        )
        .join(APIKey, UsageLog.api_key_id == APIKey.id)
        .filter(APIKey.user_id == user_id)
        .filter(UsageLog.model.isnot(None))
        .group_by(UsageLog.model)
        .order_by(func.count(UsageLog.id).desc())
    )
    result = await db.execute(stmt)
    return result.all()