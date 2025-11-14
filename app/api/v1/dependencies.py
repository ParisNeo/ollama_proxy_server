"""
Dependencies module for the Ollama Proxy Server API v1.

This module provides common dependencies used across API routes including
authentication, rate limiting, and database session management.
"""

import logging
import secrets
from typing import Optional

import redis.asyncio as redis
from fastapi import Depends, Form, Header, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_api_key
from app.crud import apikey_crud
from app.database.models import APIKey
from app.database.session import get_db
from app.schema.settings import AppSettingsModel

logger = logging.getLogger(__name__)
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)

# Module-level constants for default arguments to avoid B008 issues
DEFAULT_CSRF_TOKEN_FORM = Form(...)
DEFAULT_CSRF_TOKEN_HEADER = Header(..., alias="X-CSRF-Token")


# --- Dependency to get DB-loaded settings ---
def get_settings(request: Request) -> AppSettingsModel:
    """Get application settings from request state."""
    return request.app.state.settings


# --- CSRF Token Generation and Validation ---
async def get_csrf_token(request: Request) -> str:
    """Get CSRF token from session or create a new one."""
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_hex(32)
    return request.session["csrf_token"]


async def validate_csrf_token(request: Request, csrf_token: str = DEFAULT_CSRF_TOKEN_FORM):
    """Dependency to validate CSRF token from a form submission."""
    stored_token = await get_csrf_token(request)
    if not stored_token or not secrets.compare_digest(csrf_token, stored_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token mismatch")
    return True


async def validate_csrf_token_header(request: Request, x_csrf_token: str = DEFAULT_CSRF_TOKEN_HEADER):
    """Dependency to validate CSRF token from an X-CSRF-Token header for AJAX/fetch requests."""
    stored_token = await get_csrf_token(request)
    if not stored_token or not secrets.compare_digest(x_csrf_token, stored_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token mismatch")
    return True


# --- Login Rate Limiting Dependency ---
async def login_rate_limiter(request: Request):
    """Rate limiter for login attempts to prevent brute force attacks."""
    redis_client: redis.Redis = request.app.state.redis
    if not redis_client:
        return True

    client_ip = request.client.host
    key = f"login_fail:{client_ip}"

    try:
        current_fails = await redis_client.get(key)
        if current_fails and int(current_fails) >= 5:
            ttl = await redis_client.ttl(key)
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=f"Too many failed login attempts. Try again in {ttl} seconds.")
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.error("Could not connect to Redis for login rate limiting: %s", e)
    return True


# --- IP Filtering Dependency ---
async def ip_filter(request: Request, settings: AppSettingsModel = Depends(get_settings)):  # noqa: B008
    """Filter requests based on allowed IP addresses."""
    client_ip = request.client.host
    allowed_ips = [ip.strip() for ip in settings.allowed_ips.split(",") if ip.strip()]
    denied_ips = [ip.strip() for ip in settings.denied_ips.split(",") if ip.strip()]

    if "*" not in allowed_ips and allowed_ips and client_ip not in allowed_ips:
        logger.warning("IP address %s denied by allow-list.", client_ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP address not allowed")
    if denied_ips and client_ip in denied_ips:
        logger.warning("IP address %s denied by deny-list.", client_ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP address has been blocked")
    return True


# --- API Key Authentication Dependency ---
async def get_valid_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    auth_header: Optional[str] = Depends(api_key_header),  # noqa: B008
) -> APIKey:
    """Validate API key from Authorization header and return API key object."""
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
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format",
        ) from exc

    db_api_key = await apikey_crud.get_api_key_by_prefix(db, prefix=prefix)

    if not db_api_key:
        logger.warning("API key with prefix '%s' not found.", prefix)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key")

    if db_api_key.is_revoked:
        logger.warning("Attempt to use revoked API key with prefix '%s'.", prefix)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API Key has been revoked")

    if not db_api_key.is_active:
        logger.warning("Attempt to use disabled API key with prefix '%s'.", prefix)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API Key is disabled")

    if not verify_api_key(secret, db_api_key.hashed_key):
        logger.warning("Invalid secret for API key with prefix '%s'.", prefix)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key")

    request.state.api_key = db_api_key
    return db_api_key


# --- Rate Limiting Dependency ---
async def rate_limiter(
    request: Request,
    api_key: APIKey = Depends(get_valid_api_key),  # noqa: B008
    settings: AppSettingsModel = Depends(get_settings),  # noqa: B008
):
    """Rate limiter for API requests based on API key settings."""
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
            logger.warning("Rate limit exceeded for API key prefix: %s", api_key.key_prefix)
            ttl = await redis_client.ttl(key)
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=f"Rate limit exceeded. Try again in {ttl} seconds.", headers={"Retry-After": str(ttl)})
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.error("Could not connect to Redis for rate limiting: %s", e)
    return True


# Module-level constants for default arguments to avoid B008 issues
DEFAULT_API_KEY_HEADER = Depends(api_key_header)
DEFAULT_GET_DB = Depends(get_db)
DEFAULT_GET_SETTINGS = Depends(get_settings)
