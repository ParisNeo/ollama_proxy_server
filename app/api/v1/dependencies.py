import logging
from fastapi import Depends, HTTPException, status, Request, Form, Header
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis
import time
import secrets

from app.schema.settings import AppSettingsModel # <-- NEW
from app.database.session import get_db
from app.crud import apikey_crud
from app.core.security import verify_api_key
from app.database.models import APIKey

logger = logging.getLogger(__name__)
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)

# --- NEW: Dependency to get DB-loaded settings ---
def get_settings(request: Request) -> AppSettingsModel:
    return request.app.state.settings

# --- CSRF Token Generation and Validation ---
async def get_csrf_token(request: Request) -> str:
    """Get CSRF token from session or create a new one."""
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_hex(32)
    return request.session["csrf_token"]

async def validate_csrf_token(request: Request, csrf_token: str = Form(...)):
    """Dependency to validate CSRF token from a form submission."""
    stored_token = await get_csrf_token(request)
    if not stored_token or not secrets.compare_digest(csrf_token, stored_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token mismatch")
    return True

async def validate_csrf_token_header(request: Request, x_csrf_token: str = Header(..., alias="X-CSRF-Token")):
    """Dependency to validate CSRF token from an X-CSRF-Token header for AJAX/fetch requests."""
    stored_token = await get_csrf_token(request)
    if not stored_token or not secrets.compare_digest(x_csrf_token, stored_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token mismatch")
    return True

# --- Login Rate Limiting Dependency ---
async def login_rate_limiter(request: Request):
    redis_client: redis.Redis = request.app.state.redis
    if not redis_client:
        return True 

    client_ip = request.client.host
    key = f"login_fail:{client_ip}"
    
    try:
        current_fails = await redis_client.get(key)
        if current_fails and int(current_fails) >= 5:
            ttl = await redis_client.ttl(key)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed login attempts. Try again in {ttl} seconds."
            )
    except Exception as e:
        logger.error(f"Could not connect to Redis for login rate limiting: {e}")
    return True

# --- IP Filtering Dependency ---
async def ip_filter(request: Request, settings: AppSettingsModel = Depends(get_settings)):
    client_ip = request.client.host
    allowed_ips = [ip.strip() for ip in settings.allowed_ips.split(',') if ip.strip()]
    denied_ips = [ip.strip() for ip in settings.denied_ips.split(',') if ip.strip()]

    if "*" not in allowed_ips and allowed_ips and client_ip not in allowed_ips:
        logger.warning(f"IP address {client_ip} denied by allow-list.")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP address not allowed")
    if denied_ips and client_ip in denied_ips:
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
        prefix, secret = api_key_str.rsplit("_", 1)
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

    if not db_api_key.is_active:
        logger.warning(f"Attempt to use disabled API key with prefix '{prefix}'.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API Key is disabled"
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
    settings: AppSettingsModel = Depends(get_settings),
):
    redis_client: redis.Redis = request.app.state.redis
    if not redis_client:
        return True

    if api_key.rate_limit_requests is not None and api_key.rate_limit_window_minutes is not None:
        limit = api_key.rate_limit_requests
        window_minutes = api_key.rate_limit_window_minutes
    else:
        limit = settings.rate_limit_requests
        window_minutes = settings.rate_limit_window_minutes
    
    window = window_minutes * 60
    key = f"rate_limit:{api_key.key_prefix}"

    try:
        current_requests = await redis_client.incr(key)
        if current_requests == 1:
            await redis_client.expire(key, window)

        if current_requests > limit:
            logger.warning(f"Rate limit exceeded for API key prefix: {api_key.key_prefix}")
            ttl = await redis_client.ttl(key)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Try again in {ttl} seconds.",
                headers={"Retry-After": str(ttl)}
            )
    except Exception as e:
        logger.error(f"Could not connect to Redis for rate limiting: {e}")
    return True