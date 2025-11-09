# app/main.py
"""
Main entry point for the Ollama Proxy Server.
This version removes Alembic and uses SQLAlchemy's create_all
to initialize the database on startup.
"""

import logging
import httpx
import redis.asyncio as redis
from contextlib import asynccontextmanager
import sys 
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, Response

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.proxy import router as proxy_router
from app.api.v1.routes.admin import router as admin_router
from app.database.session import AsyncSessionLocal, engine
from app.database.base import Base
from app.database.migrations import run_all_migrations
from app.crud import user_crud, server_crud, settings_crud
from app.schema.user import UserCreate
from app.schema.server import ServerCreate
from app.schema.settings import AppSettingsModel

# --- Logging and Passlib setup ---
setup_logging(settings.LOG_LEVEL)
logger = logging.getLogger(__name__)
os.environ.setdefault("PASSLIB_DISABLE_WARNINGS", "1")

async def init_db():
    """
    Creates all database tables based on the SQLAlchemy models.
    Runs migrations first to ensure backward compatibility with older database schemas.
    """
    logger.info("Initializing database schema if it doesn't exist...")

    # Run migrations first to add any missing columns to existing tables
    await run_all_migrations(engine)

    # Then create any missing tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema is ready.")

from sqlalchemy.exc import IntegrityError

async def create_initial_admin_user() -> None:
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

async def periodic_model_refresh(app: FastAPI) -> None:
    """
    Background task that periodically refreshes model lists for all servers.
    """
    import asyncio
    
    while True:
        try:
            # Fetch the interval from app state so it can be updated live
            app_settings: AppSettingsModel = app.state.settings
            interval_minutes = app_settings.model_update_interval_minutes
            interval_seconds = interval_minutes * 60
            
            logger.info(f"Next model refresh in {interval_minutes} minutes.")
            await asyncio.sleep(interval_seconds)

            logger.info("Running periodic model refresh for all servers...")
            async with AsyncSessionLocal() as db:
                results = await server_crud.refresh_all_server_models(db)

            logger.info(
                f"Model refresh completed: {results['success']}/{results['total']} servers updated successfully"
            )

            if results['failed'] > 0:
                logger.warning(f"{results['failed']} server(s) failed to update:")
                for error in results['errors']:
                    logger.warning(f"  - {error['server_name']}: {error['error']}")

        except asyncio.CancelledError:
            logger.info("Periodic model refresh task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in periodic model refresh: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------- Startup ----------
    logger.info("Starting up Ollama Proxy Server…")
    
    # Ensure directories exist
    uploads_dir = Path("app/static/uploads")
    uploads_dir.mkdir(exist_ok=True)
    logger.info(f"Uploads directory is at: {uploads_dir.resolve()}")
    
    ssl_dir = Path(".ssl")
    ssl_dir.mkdir(exist_ok=True)
    logger.info(f"SSL storage directory is at: {ssl_dir.resolve()}")

    if settings.ADMIN_PASSWORD == "changeme":
        logger.critical("FATAL: The admin password is set to the default value 'changeme'.")
        logger.critical("Please change ADMIN_PASSWORD in your .env file or run the setup wizard and restart.")
        sys.exit(1)

    await init_db()
    
    # --- NEW: Load settings from DB ---
    async with AsyncSessionLocal() as db:
        db_settings_obj = await settings_crud.create_initial_settings(db)
        app.state.settings = AppSettingsModel.model_validate(db_settings_obj.settings_data)

    await create_initial_admin_user()

    # Configure HTTP client (timeouts are now hardcoded for simplicity)
    timeout = httpx.Timeout(10.0, read=600.0, write=600.0, pool=60.0)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100, keepalive_expiry=60.0)
    app.state.http_client = httpx.AsyncClient(timeout=timeout, limits=limits)

    try:
        db_settings: AppSettingsModel = app.state.settings
        if db_settings.redis_username and db_settings.redis_password:
            credentials = f"{db_settings.redis_username}:{db_settings.redis_password}@"
        elif db_settings.redis_username:
            credentials = f"{db_settings.redis_username}@"
        else:
            credentials = ""
        redis_url = f"redis://{credentials}{db_settings.redis_host}:{db_settings.redis_port}/0"

        app.state.redis = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        await app.state.redis.ping()
        logger.info("Successfully connected to Redis.")
    except Exception as exc:
        logger.warning(f"Redis not available – rate limiting disabled. Reason: {exc}")
        app.state.redis = None

    import asyncio
    refresh_task = asyncio.create_task(periodic_model_refresh(app))
    app.state.refresh_task = refresh_task

    # Do initial model refresh on startup
    logger.info("Performing initial model refresh on startup...")
    async with AsyncSessionLocal() as db:
        initial_results = await server_crud.refresh_all_server_models(db)
    logger.info(f"Initial model refresh: {initial_results['success']}/{initial_results['total']} servers updated")

    yield

    # ---------- Shutdown ----------
    logger.info("Shutting down…")
    if hasattr(app.state, 'refresh_task'):
        app.state.refresh_task.cancel()
        try:
            await app.state.refresh_task
        except asyncio.CancelledError:
            pass

    await app.state.http_client.aclose()
    if app.state.redis:
        await app.state.redis.close()

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

@app.get("/", include_in_schema=False, summary="Root")
def read_root():
    return RedirectResponse(url="/admin/dashboard")

if __name__ == "__main__":
    import uvicorn
    import asyncio

    async def run_server():
        """
        Connects to the DB to get settings and starts Uvicorn programmatically.
        """
        port = settings.PROXY_PORT
        ssl_keyfile = None
        ssl_certfile = None
        
        # Use the app's own DB session management to get settings
        try:
            # Run init_db which includes migrations and create_all
            await init_db()
            async with AsyncSessionLocal() as db:
                db_settings_obj = await settings_crud.get_app_settings(db)
                if not db_settings_obj:
                    # This ensures settings exist if the DB was just created
                    db_settings_obj = await settings_crud.create_initial_settings(db)

                if db_settings_obj:
                    app_settings = AppSettingsModel.model_validate(db_settings_obj.settings_data)
                    
                    if app_settings.ssl_keyfile and app_settings.ssl_certfile:
                        key_path = Path(app_settings.ssl_keyfile)
                        cert_path = Path(app_settings.ssl_certfile)
                        
                        if key_path.is_file() and cert_path.is_file():
                            ssl_keyfile = str(key_path)
                            ssl_certfile = str(cert_path)
                            logger.info(f"Uvicorn starting with HTTPS enabled.")
                        else:
                            if not key_path.is_file():
                                logger.warning(f"SSL key file not found at '{key_path}'. Starting without HTTPS.")
                            if not cert_path.is_file():
                                logger.warning(f"SSL cert file not found at '{cert_path}'. Starting without HTTPS.")
        except Exception as e:
                logger.info(f"Could not load SSL settings from DB (this is normal on first run). Reason: {e}")

        # Correct way to run uvicorn programmatically from an async function
        config = uvicorn.Config(
            "app.main:app",
            host="0.0.0.0",
            port=port,
            ssl_keyfile=ssl_keyfile,
            ssl_certfile=ssl_certfile,
            log_config=None, # Let our custom logging handle it
        )
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(run_server())