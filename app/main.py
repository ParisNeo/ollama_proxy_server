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

# --- MODIFIED LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------- Startup ----------
    logger.info("Starting up Ollama Proxy Server…")

    # Call our new database initializer first
    await init_db()

    await create_initial_admin_user()
    await create_initial_servers()

    app.state.http_client = httpx.AsyncClient()

    try:
        app.state.redis = redis.from_url(str(settings.REDIS_URL), encoding="utf-8", decode_responses=True)
        await app.state.redis.ping()
        logger.info("Successfully connected to Redis.")
    except Exception as exc:
        logger.warning(f"Redis not available – rate limiting disabled. Reason: {exc}")
        app.state.redis = None

    yield

    # ---------- Shutdown ----------
    logger.info("Shutting down…")
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