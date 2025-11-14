# app/main.py
"""
Main entry point for the Ollama Proxy Server.

This version removes Alembic and uses SQLAlchemy's create_all
to initialize the database on startup.
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.api.v1.routes.admin import router as admin_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.playground_chat import router as playground_chat_router
from app.api.v1.routes.playground_embedding import router as playground_embedding_router
from app.api.v1.routes.proxy import router as proxy_router
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.crud import server_crud, settings_crud, user_crud
from app.database.base import Base
from app.database.migrations import run_all_migrations
from app.database.session import ASYNC_SESSION_LOCAL_OLD as AsyncSessionLocal
from app.database.session import engine
from app.schema.settings import AppSettingsModel
from app.schema.user import UserCreate

# --- Suppress Pydantic 'model_' namespace warning ---
# This must be at the very top, before other app modules are imported.
# It prevents warnings when a Pydantic model field name starts with "model_".
BaseModel.model_config = ConfigDict(protected_namespaces=())

# --- Logging and Passlib setup ---
setup_logging(settings.LOG_LEVEL)
logger = logging.getLogger(__name__)
os.environ.setdefault("PASSLIB_DISABLE_WARNINGS", "1")

_db_initialized = False


async def init_db():
    """
    Create all database tables based on SQLAlchemy models.

    Runs migrations first to ensure backward compatibility with older database schemas.
    This function is designed to run only once.
    """
    # pylint: disable=global-statement
    global _db_initialized
    if _db_initialized:
        logger.debug("Database already initialized, skipping.")
        return

    logger.info("Initializing database schema...")

    # Run migrations first to add any missing columns to existing tables
    await run_all_migrations(engine)

    # Then create any missing tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)

    _db_initialized = True
    logger.info("Database schema is ready.")


async def create_initial_admin_user() -> None:
    """Create initial admin user if no users exist."""
    async with AsyncSessionLocal() as db:
        admin_user = await user_crud.get_user_by_username(db, username=settings.ADMIN_USER)
        if admin_user:
            logger.info("Admin user already exists – skipping creation.")
            return
        logger.info("Admin user not found, creating one.")
        user_in = UserCreate(username=settings.ADMIN_USER, password=settings.ADMIN_PASSWORD)
        try:
            await user_crud.create_user(db, user=user_in, is_admin=True)
            logger.info("Admin user created successfully.")
        except IntegrityError:
            logger.info("Admin user was created concurrently by another worker.")


async def periodic_model_refresh(_app: FastAPI) -> None:
    """Background task that periodically refreshes model lists for all servers."""

    while True:
        try:
            # Fetch the interval from app state so it can be updated live
            app_settings: AppSettingsModel = _app.state.settings
            interval_minutes = app_settings.model_update_interval_minutes
            interval_seconds = interval_minutes * 60

            logger.info("Next model refresh in %s minutes.", interval_minutes)
            await asyncio.sleep(interval_seconds)

            logger.info("Running periodic model refresh for all servers...")
            async with AsyncSessionLocal() as db:
                results = await server_crud.refresh_all_server_models(db)

            logger.info("Model refresh completed: %s/%s servers updated successfully", results["success"], results["total"])

            if results["failed"] > 0:
                logger.warning("%s server(s) failed to update:", results["failed"])
                for error in results["errors"]:
                    logger.warning("  - %s: %s", error["server_name"], error["error"])

        except asyncio.CancelledError:
            logger.info("Periodic model refresh task cancelled")
            break
        except (httpx.HTTPError, asyncio.TimeoutError, ConnectionError) as e:
            logger.error("Error in periodic model refresh: %s", e, exc_info=True)


def _ensure_directories():
    """Ensure required directories exist."""
    directories = [
        (Path("app/static/uploads"), "Uploads"),
        (Path(".ssl"), "SSL storage"),
        (Path("benchmarks"), "Benchmarks"),
    ]

    for directory, name in directories:
        directory.mkdir(exist_ok=True)
        logger.info("%s directory is at: %s", name, directory.resolve())


def _check_admin_password():
    """Check if admin password is still default."""

    if settings.ADMIN_PASSWORD == "changeme":  # nosec B105 - false positive
        logger.critical("FATAL: The admin password is set to default value 'changeme'.")
        logger.critical("Please change ADMIN_PASSWORD in your .env file or run setup wizard and restart.")
        sys.exit(1)


async def _setup_redis_connection(_app: FastAPI):
    """Set up Redis connection if available."""
    try:
        db_settings: AppSettingsModel = _app.state.settings
        if db_settings.redis_username and db_settings.redis_password:
            credentials = f"{db_settings.redis_username}:{db_settings.redis_password}@"
        elif db_settings.redis_username:
            credentials = f"{db_settings.redis_username}@"
        else:
            credentials = ""
        redis_url = f"redis://{credentials}{db_settings.redis_host}:{db_settings.redis_port}/0"

        _app.state.redis = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        await _app.state.redis.ping()
        logger.info("Successfully connected to Redis.")
    except (redis.ConnectionError, redis.TimeoutError, ConnectionRefusedError) as exc:
        logger.warning("Redis not available – rate limiting disabled. Reason: %s", exc)
        _app.state.redis = None


async def _setup_http_client(_app: FastAPI):
    """Set up HTTP client with appropriate timeouts and limits."""
    timeout = httpx.Timeout(10.0, read=600.0, write=600.0, pool=60.0)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100, keepalive_expiry=60.0)
    _app.state.http_client = httpx.AsyncClient(timeout=timeout, limits=limits)


async def setup_redis_connection(_app: FastAPI):
    """Set up Redis connection if available."""
    try:
        db_settings: AppSettingsModel = app.state.settings
        if db_settings.redis_username and db_settings.redis_password:
            credentials = f"{db_settings.redis_username}:{db_settings.redis_password}@"
        elif db_settings.redis_username:
            credentials = f"{db_settings.redis_username}@"
        else:
            credentials = ""
        redis_url = f"redis://{credentials}{db_settings.redis_host}:{db_settings.redis_port}/0"

        _app.state.redis = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        await _app.state.redis.ping()
        logger.info("Successfully connected to Redis.")
    except (redis.ConnectionError, redis.TimeoutError, ConnectionRefusedError) as exc:
        logger.warning("Redis not available – rate limiting disabled. Reason: %s", exc)
        _app.state.redis = None


async def _perform_initial_model_refresh():
    """Perform initial model refresh on startup."""
    logger.info("Performing initial model refresh on startup...")
    async with AsyncSessionLocal() as db:
        initial_results = await server_crud.refresh_all_server_models(db)
    logger.info("Initial model refresh: %s/%s servers updated", initial_results["success"], initial_results["total"])


async def _cleanup_resources(_app: FastAPI):
    """Cleanup resources during shutdown."""
    if hasattr(_app.state, "refresh_task"):
        _app.state.refresh_task.cancel()
        try:
            await _app.state.refresh_task
        except asyncio.CancelledError:
            pass

    await _app.state.http_client.aclose()
    if _app.state.redis:
        await _app.state.redis.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifespan manager."""
    # ---------- Startup ----------
    logger.info("Starting up Ollama Proxy Server…")

    _ensure_directories()
    _check_admin_password()

    await init_db()

    # --- Load settings from DB ---
    async with AsyncSessionLocal() as db:
        db_settings_obj = await settings_crud.create_initial_settings(db)
        _app.state.settings = AppSettingsModel.model_validate(db_settings_obj.settings_data)

    await create_initial_admin_user()
    await _setup_http_client(_app)
    await setup_redis_connection(_app)

    refresh_task = asyncio.create_task(periodic_model_refresh(_app))
    _app.state.refresh_task = refresh_task

    await _perform_initial_model_refresh()

    yield

    # ---------- Shutdown ----------
    logger.info("Shutting down…")
    await _cleanup_resources(_app)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="A secure, high‑performance proxy and load balancer for Ollama.",
    redoc_url=None,
    openapi_url="/api/v1/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to the response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    csp_policy = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.tailwindcss.com https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "img-src 'self' https: data:; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    response.headers["Content-Security-Policy"] = csp_policy
    return response


app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(health_router, prefix="/api/v1", tags=["Health"])
app.include_router(proxy_router, prefix="/api", tags=["Ollama Proxy"])
app.include_router(admin_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(playground_chat_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(playground_embedding_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)


@app.get("/", include_in_schema=False, summary="Root")
def read_root():
    """Redirect root to admin dashboard."""
    return RedirectResponse(url="/admin/dashboard")


if __name__ == "__main__":
    import uvicorn

    async def run_server():
        """Connect to the DB to get settings and start Uvicorn programmatically."""
        port = settings.PROXY_PORT
        ssl_keyfile = None
        ssl_certfile = None

        try:
            # Run init_db once to ensure DB exists for reading SSL settings.
            await init_db()
            async with AsyncSessionLocal() as db:
                db_settings_obj = await settings_crud.get_app_settings(db)
                if not db_settings_obj:
                    db_settings_obj = await settings_crud.create_initial_settings(db)

                if db_settings_obj:
                    app_settings = AppSettingsModel.model_validate(db_settings_obj.settings_data)

                    if app_settings.ssl_keyfile and app_settings.ssl_certfile:
                        key_path = Path(app_settings.ssl_keyfile)
                        cert_path = Path(app_settings.ssl_certfile)

                        if key_path.is_file() and cert_path.is_file():
                            ssl_keyfile = str(key_path)
                            ssl_certfile = str(cert_path)
                        else:
                            if not key_path.is_file():
                                logger.warning("SSL key file not found at '%s'. Starting without HTTPS.", key_path)
                            if not cert_path.is_file():
                                logger.warning("SSL cert file not found at '%s'. Starting without HTTPS.", cert_path)
        except OSError as e:
            logger.info("Could not load SSL settings from DB (this is normal on first run). Reason: %s", e)

        # --- User-friendly startup banner ---
        protocol = "https" if ssl_keyfile and ssl_certfile else "http"

        # This function will be called after Uvicorn starts up
        def after_start():
            print("\n" + "=" * 60)
            print("🚀 Ollama Proxy Fortress is running! 🚀")
            print("=" * 60)
            print(f"✅ Version: {settings.APP_VERSION}")
            print(f"✅ Mode: {'Production (HTTPS)' if protocol == 'https' else 'Development (HTTP)'}")
            print(f"✅ Listening on port: {port}")
            print("\nTo access the admin dashboard, open your web browser to:")
            print(f"    {protocol}://127.0.0.1:{port}/admin/dashboard")
            print(f"    or {protocol}://localhost:{port}/admin/dashboard")
            print("\nTo stop the server, press CTRL+C in this window.")
            print("=" * 60 + "\n")
            print("Note: Log messages from 'uvicorn.error' are for general server events and do not necessarily indicate an error.\n")

        # Correct way to run uvicorn programmatically
        config = uvicorn.Config(
            "app.main:app",
            host="0.0.0.0",  # nosec B104 - make this default localhost ?
            port=port,
            ssl_keyfile=ssl_keyfile,
            ssl_certfile=ssl_certfile,
            log_config=None,  # Let our custom logging handle it
        )
        server = uvicorn.Server(config)

        # A bit of a workaround to print banner after Uvicorn's own startup messages
        original_startup = server.startup

        async def new_startup(*args, **kwargs):
            await original_startup(*args, **kwargs)
            after_start()

        server.startup = new_startup

        await server.serve()

    asyncio.run(run_server())
