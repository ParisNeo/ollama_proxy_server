# app/crud/log_crud.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, text, Date
from app.database.models import UsageLog, APIKey, User, OllamaServer
import datetime

async def create_usage_log(
    db: AsyncSession, *, api_key_id: int, endpoint: str, status_code: int, server_id: int | None, model: str | None = None
) -> UsageLog:
    # Validate inputs to prevent injection
    if not isinstance(api_key_id, int) or api_key_id <= 0:
        raise ValueError("Invalid api_key_id")
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ValueError("Invalid endpoint")
    if not isinstance(status_code, int) or status_code < 100 or status_code > 599:
        raise ValueError("Invalid status_code")
    if model is not None and (not isinstance(model, str) or len(model) > 256):
        raise ValueError("Invalid model name")
        
    db_log = UsageLog(
        api_key_id=api_key_id,
        endpoint=endpoint[:512],  # Limit length
        status_code=status_code,
        server_id=server_id,
        model=model[:256] if model else None
    )
    db.add(db_log)
    await db.commit()
    await db.refresh(db_log)
    return db_log

async def get_usage_statistics(db: AsyncSession, sort_by: str = "request_count", sort_order: str = "desc"):
    """
    Returns aggregated usage statistics for all API keys, with sorting.
    """
    # Whitelist allowed sort columns to prevent injection
    allowed_sort_columns = {
        "username": User.username,
        "key_name": APIKey.key_name,
        "key_prefix": APIKey.key_prefix,
        "request_count": func.count(UsageLog.id),
    }
    
    # Default to request_count if invalid column provided
    sort_column = allowed_sort_columns.get(sort_by, func.count(UsageLog.id))

    # Validate sort_order
    if sort_order.lower() not in ("asc", "desc"):
        sort_order = "desc"

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
    # Validate days parameter
    try:
        days = int(days)
        if days < 1 or days > 365:
            days = 30
    except (ValueError, TypeError):
        days = 30
    
    start_date = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    
    # Use SQLAlchemy's func.date with explicit type - SAFE from injection
    # The type_=Date is a SQLAlchemy type object, not user input
    date_column = func.date(UsageLog.request_timestamp).label("date")
    
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
    # Use safe SQLAlchemy constructs only
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
    # Validate inputs
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError("Invalid user_id")
    except (ValueError, TypeError):
        raise ValueError("Invalid user_id")
        
    try:
        days = int(days)
        if days < 1 or days > 365:
            days = 30
    except (ValueError, TypeError):
        days = 30
        
    start_date = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    
    # Safe SQLAlchemy construct
    date_column = func.date(UsageLog.request_timestamp).label("date")
    
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
    # Validate user_id
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError("Invalid user_id")
    except (ValueError, TypeError):
        raise ValueError("Invalid user_id")
        
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
    # Validate user_id
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError("Invalid user_id")
    except (ValueError, TypeError):
        raise ValueError("Invalid user_id")
        
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
    # Validate user_id
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError("Invalid user_id")
    except (ValueError, TypeError):
        raise ValueError("Invalid user_id")
        
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
