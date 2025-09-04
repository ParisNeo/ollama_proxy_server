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
from app.database.base import Base
from app.crud import user_crud, server_crud
from app.schema.user import UserCreate
from app.schema.server import ServerCreate

# Setup structured logging
setup_logging(settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


async def create_initial_admin_user():
    async with AsyncSessionLocal() as db:
        admin_user = await user_crud.get_user_by_username(db, username=settings.ADMIN_USER)
        if not admin_user:
            logger.info("Admin user not found, creating one.")
            user_in = UserCreate(username=settings.ADMIN_USER, password=settings.ADMIN_PASSWORD)
            await user_crud.create_user(db, user=user_in, is_admin=True)
            logger.info("Admin user created successfully.")
        else:
            logger.info("Admin user already exists.")

async def create_initial_servers():
    """
    On first startup, populate the database with servers from the .env file.
    This allows for easy bootstrapping. After this, servers are managed via the UI.
    """
    async with AsyncSessionLocal() as db:
        existing_servers = await server_crud.get_servers(db, limit=1)
        if not existing_servers:
            logger.info("No servers found in the database. Bootstrapping from .env settings.")
            for i, server_url in enumerate(settings.OLLAMA_SERVERS):
                server_in = ServerCreate(name=f"Default Server {i+1}", url=server_url)
                await server_crud.create_server(db, server=server_in)
            logger.info(f"{len(settings.OLLAMA_SERVERS)} server(s) bootstrapped successfully.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup
    logger.info("Starting up Ollama Proxy Server...")
    
    # Database initialization is now handled exclusively by Alembic in the setup scripts.
    # We no longer call `create_all` here.

    await create_initial_admin_user()
    await create_initial_servers()
    
    # Initialize the httpx client
    app.state.http_client = httpx.AsyncClient()
    
    # Initialize Redis client
    try:
        app.state.redis = redis.from_url(str(settings.REDIS_URL), encoding="utf-8", decode_responses=True)
        await app.state.redis.ping()
        logger.info("Successfully connected to Redis.")
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        app.state.redis = None
    
    yield
    # On shutdown
    logger.info("Shutting down...")
    await app.state.http_client.aclose()
    if app.state.redis:
        await app.state.redis.close()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="A secure, high-performance proxy and load balancer for Ollama.",
    redoc_url=None,
    openapi_url="/api/v1/openapi.json",
    lifespan=lifespan,
)

# --- Middleware ---
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

# --- Static Files ---
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# --- Routers ---
app.include_router(health_router, prefix="/api/v1", tags=["Health"])
app.include_router(proxy_router, prefix="/api", tags=["Ollama Proxy"])
app.include_router(admin_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)


@app.get("/", summary="Root", include_in_schema=False)
def read_root():
    """Redirects to the admin dashboard."""
    return RedirectResponse(url="/admin/dashboard")