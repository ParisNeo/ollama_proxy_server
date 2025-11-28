# app/main.py
"""
Main entry point for the Ollama Proxy Server.
This version removes Alembic and uses SQLAlchemy's create_all
to initialize the database on startup.
"""
import logging
import os
import sys
from pydantic import BaseModel, ConfigDict

# --- Suppress Pydantic 'model_' namespace warning ---
# This must be at the very top, before other app modules are imported.
# It prevents warnings when a Pydantic model field name starts with "model_".
BaseModel.model_config = ConfigDict(protected_namespaces=())

import httpx
import redis.asyncio as redis
from contextlib import asynccontextmanager
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, Response

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.proxy import router as proxy_router
from app.api.v1.routes.openai_compat import router as openai_compat_router
from app.api.v1.routes.admin import router as admin_router
from app.api.v1.routes.playground_chat import router as playground_chat_router
from app.api.v1.routes.playground_embedding import router as playground_embedding_router
from app.api.v1.routes.search import router as search_router
from app.api.v1.routes.conversations import router as conversations_router
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

_db_initialized = False
async def init_db():
    """
    Creates all database tables based on the SQLAlchemy models.
    Runs migrations first to ensure backward compatibility with older database schemas.
    This function is designed to run only once.
    """
    global _db_initialized
    if _db_initialized:
        logger.debug("Database already initialized, skipping.")
        return

    logger.info("Initializing database schema...")

    # Run migrations first to add any missing columns to existing tables
    await run_all_migrations(engine)

    # Then create any missing tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    _db_initialized = True
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

    benchmarks_dir = Path("benchmarks")
    benchmarks_dir.mkdir(exist_ok=True)
    logger.info(f"Benchmarks directory is at: {benchmarks_dir.resolve()}")

    if settings.ADMIN_PASSWORD == "changeme":
        logger.critical("FATAL: The admin password is set to the default value 'changeme'.")
        logger.critical("Please change ADMIN_PASSWORD in your .env file or run the setup wizard and restart.")
        sys.exit(1)

    await init_db()
    
    # --- NEW: Load settings from DB ---
    async with AsyncSessionLocal() as db:
        db_settings_obj = await settings_crud.create_initial_settings(db)
        
        # Fix corrupted redis_port BEFORE validation (to prevent validation errors)
        settings_dict = dict(db_settings_obj.settings_data) if db_settings_obj.settings_data else {}
        redis_port_value = settings_dict.get('redis_port', 6379)
        
        logger.info(f"Loading settings from database. Current redis_port value: {redis_port_value} (type: {type(redis_port_value)})")
        
        # ALWAYS check and fix redis_port before validation
        fixed_port = 6379
        if isinstance(redis_port_value, str):
            try:
                port_int = int(redis_port_value)
                if 1 <= port_int <= 65535:
                    fixed_port = port_int
                    logger.debug(f"Redis port string '{redis_port_value}' converted to int {port_int}")
                else:
                    logger.warning(f"Redis port {port_int} out of range, using 6379")
            except (ValueError, TypeError) as e:
                logger.warning(f"Found invalid Redis port value '{redis_port_value}' in database (cannot convert to int: {e}), fixing to 6379")
        elif isinstance(redis_port_value, int) and (1 <= redis_port_value <= 65535):
            fixed_port = redis_port_value
            logger.debug(f"Redis port is already valid int: {redis_port_value}")
        else:
            logger.warning(f"Found invalid Redis port value '{redis_port_value}' (type: {type(redis_port_value)}) in database, fixing to 6379")
        
        # Always update the dict with the fixed port if it changed
        if redis_port_value != fixed_port:
            logger.info(f"Fixing redis_port: {redis_port_value} -> {fixed_port}")
            settings_dict['redis_port'] = fixed_port
            db_settings_obj.settings_data = settings_dict
            await db.commit()
            await db.refresh(db_settings_obj)
            logger.info(f"Redis port value has been corrected in the database: {redis_port_value} -> {fixed_port}")
        else:
            logger.debug(f"Redis port value is already correct: {fixed_port}")
        
        # Now validate with the fixed value
        try:
            logger.debug("Validating settings with Pydantic...")
            app.state.settings = AppSettingsModel.model_validate(db_settings_obj.settings_data)
            logger.debug(f"Settings validated successfully. redis_port = {app.state.settings.redis_port}")
        except Exception as e:
            logger.error(f"Error validating settings: {e}. Attempting to fix and retry...")
            # Force fix redis_port and try again
            settings_dict['redis_port'] = 6379
            db_settings_obj.settings_data = settings_dict
            await db.commit()
            await db.refresh(db_settings_obj)
            try:
                app.state.settings = AppSettingsModel.model_validate(db_settings_obj.settings_data)
                logger.info("Successfully validated settings after forcing Redis port to 6379")
            except Exception as e2:
                logger.error(f"Still failed after fix: {e2}. Using default settings.")
                app.state.settings = AppSettingsModel()

    await create_initial_admin_user()

    # Configure HTTP client (timeouts are now hardcoded for simplicity)
    timeout = httpx.Timeout(10.0, read=600.0, write=600.0, pool=60.0)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100, keepalive_expiry=60.0)
    app.state.http_client = httpx.AsyncClient(timeout=timeout, limits=limits)

    try:
        db_settings: AppSettingsModel = app.state.settings
        
        # Get redis_port - validator should have already converted it, but be extra safe
        redis_port_value = getattr(db_settings, 'redis_port', 6379)
        
        # Ensure redis_port is an integer (validator should handle this, but double-check)
        if isinstance(redis_port_value, str):
            try:
                redis_port = int(redis_port_value)
            except (ValueError, TypeError):
                logger.warning(f"Invalid Redis port value '{redis_port_value}' (string), using default 6379")
                redis_port = 6379
        elif isinstance(redis_port_value, int):
            redis_port = redis_port_value
        else:
            logger.warning(f"Invalid Redis port type '{type(redis_port_value)}', using default 6379")
            redis_port = 6379
        
        # Validate port is in valid range
        if not (1 <= redis_port <= 65535):
            logger.warning(f"Redis port {redis_port} out of valid range (1-65535), using default 6379")
            redis_port = 6379
        
        # Build credentials with URL encoding (passwords may contain special chars like #, @, !)
        # Redis with --requirepass uses password-only auth (no username needed)
        from urllib.parse import quote_plus
        encoded_password = None
        if db_settings.redis_password:
            encoded_password = quote_plus(db_settings.redis_password)
            # Use password-only format for --requirepass (most common)
            # Format: redis://:password@host:port
            credentials = f":{encoded_password}@"
        else:
            # No authentication
            credentials = ""
        
        redis_url = f"redis://{credentials}{db_settings.redis_host}:{redis_port}/0"
        # Log with masked password for security
        if encoded_password and db_settings.redis_password:
            safe_url = redis_url.replace(encoded_password, '*' * len(db_settings.redis_password))
        else:
            safe_url = redis_url
        logger.debug(f"Attempting Redis connection to: {safe_url}")

        app.state.redis = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        await app.state.redis.ping()
        logger.info(f"Successfully connected to Redis at {db_settings.redis_host}:{redis_port}")
    except Exception as exc:
        logger.warning(f"Redis not available – rate limiting disabled. Reason: {exc}")
        app.state.redis = None
    except Exception as exc:
        logger.warning(f"Redis not available – rate limiting disabled. Reason: {exc}")
        app.state.redis = None

    import asyncio
    refresh_task = asyncio.create_task(periodic_model_refresh(app))
    app.state.refresh_task = refresh_task

    # Initialize RAG/embedding service for conversation search
    from app.core.rag_service import RAGService
    try:
        app.state.rag_service = RAGService()
        await app.state.rag_service.initialize()
        logger.info("RAG/embedding service initialized")
    except Exception as e:
        logger.warning(f"RAG service not available: {e}. Conversation search will be limited.")
        app.state.rag_service = None

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
    
    # RAG service cleanup is handled automatically by ChromaDB
    
    # Close search proxy
    from app.core.simple_search import _search_proxy
    if _search_proxy:
        await _search_proxy.close()

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
# OpenAI-compatible endpoints at root level for clients like MSTY
app.include_router(openai_compat_router, prefix="", tags=["OpenAI Compatible"])
app.include_router(admin_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(playground_chat_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(playground_embedding_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(search_router, tags=["Search"], include_in_schema=False)
app.include_router(conversations_router, prefix="/admin/api", tags=["Conversations"], include_in_schema=False)

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
                                logger.warning(f"SSL key file not found at '{key_path}'. Starting without HTTPS.")
                            if not cert_path.is_file():
                                logger.warning(f"SSL cert file not found at '{cert_path}'. Starting without HTTPS.")
        except Exception as e:
                logger.info(f"Could not load SSL settings from DB (this is normal on first run). Reason: {e}")

        # --- User-friendly startup banner ---
        protocol = "https" if ssl_keyfile and ssl_certfile else "http"
        
        # This function will be called after Uvicorn starts up
        def after_start():
            try:
                # Try to print with emoji, but fallback if Windows console can't handle it
                banner = "\n" + "="*60 + "\n"
                banner += "🚀 Ollama Proxy Fortress is running! 🚀\n"
                banner += "="*60 + "\n"
                banner += f"✅ Version: {settings.APP_VERSION}\n"
                banner += f"✅ Mode: {'Production (HTTPS)' if protocol == 'https' else 'Development (HTTP)'}\n"
                banner += f"✅ Listening on port: {port}\n"
                print(banner)
            except UnicodeEncodeError:
                # Fallback for Windows console that doesn't support emoji
                print("\n" + "="*60)
                print(">>> Ollama Proxy Fortress is running! <<<")
                print("="*60)
                print(f"[OK] Version: {settings.APP_VERSION}")
                print(f"[OK] Mode: {'Production (HTTPS)' if protocol == 'https' else 'Development (HTTP)'}")
                print(f"[OK] Listening on port: {port}")
            print("\nTo access the admin dashboard, open your web browser to:")
            print(f"    {protocol}://127.0.0.1:{port}/admin/dashboard")
            print(f"    or {protocol}://localhost:{port}/admin/dashboard")
            print("\nTo stop the server, press CTRL+C in this window.")
            print("="*60 + "\n")
            print("Note: Log messages from 'uvicorn.error' are for general server events and do not necessarily indicate an error.\n")


        # Correct way to run uvicorn programmatically
        # Ensure WebSocket support is available
        try:
            import websockets
            import wsproto
            logger.debug("WebSocket libraries available: websockets, wsproto")
        except ImportError as e:
            logger.warning(f"WebSocket libraries not available: {e}. Some features may not work.")
            logger.warning("Install with: pip install 'uvicorn[standard]' websockets wsproto")
        
        config = uvicorn.Config(
            "app.main:app",
            host="0.0.0.0",
            port=port,
            ssl_keyfile=ssl_keyfile,
            ssl_certfile=ssl_certfile,
            log_config=None, # Let our custom logging handle it
            ws="auto",  # Auto-detect WebSocket library
            reload=True,  # Enable hot reload for development
            reload_dirs=["app"],  # Watch app directory for changes
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
