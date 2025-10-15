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

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.proxy import router as proxy_router
from app.api.v1.routes.admin import router as admin_router
from app.database.session import AsyncSessionLocal, engine
from app.database.base import Base # <-- Import Base
from app.crud import user_crud, server_crud
from app.schema.user import UserCreate
from app.schema.server import ServerCreate

# --- Logging and Passlib setup ---
setup_logging(settings.LOG_LEVEL)
logger = logging.getLogger(__name__)
import os
os.environ.setdefault("PASSLIB_DISABLE_WARNINGS", "1")

# --- NEW FUNCTION: Initialize Database ---
async def init_db():
    """
    Creates all database tables based on the SQLAlchemy models.
    This replaces the need for Alembic migrations for initial setup.
    """
    logger.info("Initializing database schema if it doesn't exist...")
    async with engine.begin() as conn:
        # This command creates tables only if they don't already exist.
        await conn.run_sync(Base.metadata.create_all)

        # Manual migration: Add new columns if they don't exist
        # This is a simple approach for SQLite - for production with PostgreSQL, use Alembic
        def add_missing_columns(connection):
            from sqlalchemy import text, inspect
            inspector = inspect(connection)

            # Check and add 'model' column to usage_logs
            columns = [col['name'] for col in inspector.get_columns('usage_logs')]
            if 'model' not in columns:
                logger.info("Adding 'model' column to usage_logs table...")
                connection.execute(text("ALTER TABLE usage_logs ADD COLUMN model VARCHAR"))
                logger.info("Column 'model' added successfully.")

            # Check and add 'available_models' column to ollama_servers
            columns = [col['name'] for col in inspector.get_columns('ollama_servers')]
            if 'available_models' not in columns:
                logger.info("Adding 'available_models' column to ollama_servers table...")
                connection.execute(text("ALTER TABLE ollama_servers ADD COLUMN available_models JSON"))
                logger.info("Column 'available_models' added successfully.")

            # Check and add 'models_last_updated' column to ollama_servers
            if 'models_last_updated' not in columns:
                logger.info("Adding 'models_last_updated' column to ollama_servers table...")
                connection.execute(text("ALTER TABLE ollama_servers ADD COLUMN models_last_updated DATETIME"))
                logger.info("Column 'models_last_updated' added successfully.")

        await conn.run_sync(add_missing_columns)

    logger.info("Database schema is ready.")
# --- END NEW FUNCTION ---

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

async def create_initial_servers() -> None:
    async with AsyncSessionLocal() as db:
        existing = await server_crud.get_servers(db, limit=1)
        if existing:
            logger.info("Ollama servers already present – skipping bootstrap.")
            return
        logger.info("No servers found – bootstrapping from .env.")
        for i, server_url in enumerate(settings.OLLAMA_SERVERS):
            server_in = ServerCreate(name=f"Default Server {i + 1}", url=server_url)
            try:
                await server_crud.create_server(db, server=server_in)
            except IntegrityError:
                logger.warning(f"Server {server_url} already exists (race condition).")
        logger.info(f"{len(settings.OLLAMA_SERVERS)} server(s) bootstrapped successfully.")

async def periodic_model_refresh() -> None:
    """
    Background task that periodically refreshes model lists for all servers.
    """
    import asyncio

    interval_seconds = settings.MODEL_REFRESH_INTERVAL_MINUTES * 60
    logger.info(f"Starting periodic model refresh task (every {settings.MODEL_REFRESH_INTERVAL_MINUTES} minutes)")

    while True:
        try:
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

# --- MODIFIED LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------- Startup ----------
    logger.info("Starting up Ollama Proxy Server…")

    # Call our new database initializer first
    await init_db()

    await create_initial_admin_user()
    await create_initial_servers()

    # Configure HTTP client using settings
    timeout = httpx.Timeout(
        connect=settings.HTTPX_CONNECT_TIMEOUT,
        read=settings.HTTPX_READ_TIMEOUT,
        write=settings.HTTPX_WRITE_TIMEOUT,
        pool=settings.HTTPX_POOL_TIMEOUT,
    )
    limits = httpx.Limits(
        max_keepalive_connections=settings.HTTPX_MAX_KEEPALIVE_CONNECTIONS,
        max_connections=settings.HTTPX_MAX_CONNECTIONS,
        keepalive_expiry=settings.HTTPX_KEEPALIVE_EXPIRY,
    )
    app.state.http_client = httpx.AsyncClient(timeout=timeout, limits=limits)

    try:
        app.state.redis = redis.from_url(str(settings.REDIS_URL), encoding="utf-8", decode_responses=True)
        await app.state.redis.ping()
        logger.info("Successfully connected to Redis.")
    except Exception as exc:
        logger.warning(f"Redis not available – rate limiting disabled. Reason: {exc}")
        app.state.redis = None

    # Start background task for periodic model refresh
    import asyncio
    refresh_task = asyncio.create_task(periodic_model_refresh())
    app.state.refresh_task = refresh_task

    # Do initial model refresh on startup
    logger.info("Performing initial model refresh on startup...")
    async with AsyncSessionLocal() as db:
        initial_results = await server_crud.refresh_all_server_models(db)
    logger.info(f"Initial model refresh: {initial_results['success']}/{initial_results['total']} servers updated")

    yield

    # ---------- Shutdown ----------
    logger.info("Shutting down…")

    # Cancel the background refresh task
    if hasattr(app.state, 'refresh_task'):
        app.state.refresh_task.cancel()
        try:
            await app.state.refresh_task
        except asyncio.CancelledError:
            pass

    await app.state.http_client.aclose()
    if app.state.redis:
        await app.state.redis.close()
# --- END MODIFIED LIFESPAN ---

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="A secure, high‑performance proxy and load balancer for Ollama.",
    redoc_url=None,
    openapi_url="/api/v1/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(health_router, prefix="/api/v1", tags=["Health"])
app.include_router(proxy_router, prefix="/api", tags=["Ollama Proxy"])
app.include_router(admin_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)

@app.get("/", include_in_schema=False, summary="Root")
def read_root():
    return RedirectResponse(url="/admin/dashboard")