import logging
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

from app.core.config import settings
from app.database.session import get_db
from app.crud import apikey_crud
from app.core.security import verify_api_key
from app.database.models import APIKey

logger = logging.getLogger(__name__)
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


# --- IP Filtering Dependency ---
async def ip_filter(request: Request):
    client_ip = request.client.host
    if settings.ALLOWED_IPS and client_ip not in settings.ALLOWED_IPS:
        logger.warning(f"IP address {client_ip} denied by allow-list.")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP address not allowed")
    if settings.DENIED_IPS and client_ip in settings.DENIED_IPS:
        logger.warning(f"IP address {client_ip} denied by deny-list.")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP address has been blocked")
    return True

# --- API Key Authentication Dependency ---
async def get_valid_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth_header: str = Depends(api_key_header),
) -> APIKey:
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is missing",
        )

    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme. Use 'Bearer <api_key>'.",
        )

    api_key_str = auth_header.split(" ")[1]

    try:
        prefix, secret = api_key_str.split("_", 1)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format",
        )

    db_api_key = await apikey_crud.get_api_key_by_prefix(db, prefix=prefix)

    if not db_api_key:
        logger.warning(f"API key with prefix '{prefix}' not found.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key"
        )

    if db_api_key.is_revoked:
        logger.warning(f"Attempt to use revoked API key with prefix '{prefix}'.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API Key has been revoked"
        )

    if not verify_api_key(secret, db_api_key.hashed_key):
        logger.warning(f"Invalid secret for API key with prefix '{prefix}'.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key"
        )

    request.state.api_key = db_api_key
    return db_api_key

# --- Rate Limiting Dependency ---
async def rate_limiter(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),
):
    redis_client: redis.Redis = request.app.state.redis
    if not redis_client:
        logger.warning("Redis client not available, skipping rate limit check.")
        return True

    limit = settings.RATE_LIMIT_REQUESTS
    window = settings.RATE_LIMIT_WINDOW_MINUTES * 60  # in seconds
    key = f"rate_limit:{api_key.key_prefix}"

    try:
        current_requests = await redis_client.incr(key)
        if current_requests == 1:
            await redis_client.expire(key, window)

        if current_requests > limit:
            logger.warning(f"Rate limit exceeded for API key prefix: {api_key.key_prefix}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Try again in {await redis_client.ttl(key)} seconds.",
            )
    except Exception as e:
        logger.error(f"Could not connect to Redis for rate limiting: {e}")
        # Fail open: if Redis is down, allow the request to proceed.
    return True