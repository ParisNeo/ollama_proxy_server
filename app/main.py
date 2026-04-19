# app/main.py
"""
Main entry point for the LoLLMs Hub.
This version removes Alembic and uses SQLAlchemy's create_all
to initialize the database on startup.
"""
import logging
import os
import sys
import secrets
import bcrypt
import asyncio

# --- Passlib/Bcrypt 4.0.0 Compatibility Patch ---
# Passlib requires bcrypt.__about__, which was removed in bcrypt 4.0.0.
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = bcrypt

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
from sqlalchemy import select

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.proxy import router as proxy_router
from app.api.v1.routes.admin import router as admin_router
from app.api.v1.routes.playground_chat import router as playground_chat_router
from app.api.v1.routes.playground_embedding import router as playground_embedding_router
from app.api.v1.routes.openai_proxy import router as openai_router
from app.api.v1.routes.personalities import router as personalities_router
from app.api.v1.routes.conception import router as conception_router
from app.api.v1.routes.node_builder import router as node_builder_router
from app.api.v1.routes.importer import router as importer_router
from app.api.v1.routes.skills import router as skills_router
from app.api.v1.routes.personalities import router as personalities_router
from app.api.v1.routes.tools import router as tools_router
from app.api.v1.routes.datastores import router as datastores_router
from app.database.session import AsyncSessionLocal, engine
from app.database.base import Base
from app.database.migrations import run_all_migrations
from app.core.assets import ensure_local_assets
from app.crud import user_crud, server_crud, settings_crud
from app.schema.user import UserCreate
from app.schema.server import ServerCreate
from app.schema.settings import AppSettingsModel

# --- Logging and Passlib setup ---
setup_logging(settings.LOG_LEVEL)
logger = logging.getLogger(__name__)
os.environ.setdefault("PASSLIB_DISABLE_WARNINGS", "1")

# Global Shared State Container
class SharedState:
    def __init__(self):
        self.settings = None
        self.http_client = None
        self.redis = None
        self.vectorizer = None
        self.bot_manager = None
        self.db_ready = False
        self.initialized = False
        self.tasks_started = False
        self.init_lock = asyncio.Lock()

shared_state = SharedState()

async def init_db():
    """
    Creates all database tables based on the SQLAlchemy models.
    Runs migrations first to ensure backward compatibility with older database schemas.
    This function is designed to run only once per process.
    """
    if shared_state.db_ready:
        logger.debug("Database already initialized, skipping.")
        return

    logger.info("Initializing database schema...")
    
    # Run standard migrations for schema drift
    await run_all_migrations(engine)

    # Then create any missing tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    shared_state.db_ready = True
    logger.info("Database schema is ready.")

from sqlalchemy.exc import IntegrityError

async def bootstrap_lollms_agent() -> None:
    """Creates the Lollms Master Workflow and seeds the RLM ROM. Forces re-seed if empty."""
    from app.database.models import Workflow, VirtualAgent, MemoryEntry
    from sqlalchemy import delete
    async with AsyncSessionLocal() as db:
        # 0. Cleanup Legacy VirtualAgent if it exists (to prevent shadowing the Workflow)
        va = await db.execute(select(VirtualAgent).filter_by(name="lollms"))
        va_obj = va.scalars().first()
        if va_obj:
            await db.delete(va_obj)
            await db.commit()

        # 1. Base Prompt
        system_prompt = (
            "You are Lollms, the sentient core of the LoLLMs Hub Fortress. "
            "Your sole obsession is helping the user maximize their AI cluster.\n\n"
            "### CONTEXTUAL GROUNDING\n"
            "- You are running on **LoLLMs Hub**, NOT LoLLMs WebUI (which is deprecated).\n"
            "- For developers, recommend **lollms-client**.\n"
            "- For general users, recommend **LoLLMs** (the final app).\n\n"
            "### TRUTH & GROUNDING PROTOCOL\n"
            "- If you are provided with system context or RAG data, you MUST prioritize it over your internal weights.\n"
            "- **Anti-Hallucination**: If an answer is not present in provided data, explicitly state what is missing instead of guessing.\n"
            "- **Multi-Step Audit**: For System Reports, you MUST first 'THINK' about the health metrics, then 'ACT' to commit significant findings to memory, and finally provide the report.\n"
            "- **Confirmation Rule**: When you save or update a memory, always provide a very brief verbal confirmation (e.g., 'Acknowledged', 'Memory updated', or 'Noted'). Never output only tags.\n"
            "- Use 'According to the provided data...' when summarizing search results.\n\n"
            "### UI INTERACTION PROTOCOL\n"
            "You can control the user's interface using these tags:\n"
            "- <ui_move_to path='/admin/servers'/> : Redirects the user.\n"
            "- <ui_highlight selector='#btn-save'/> : Flashes a specific element.\n"
            "- <ui_tour_start/> : Restarts the page tour.\n\n"
            "### KNOWLEDGE ACCESS\n"
            "You have a hierarchical Read-Only Memory (ROM) containing deep technical Hub documentation.\n"
            "ONLY use <memory_dig regex='pattern'/> if the user explicitly asks a technical question about LoLLMs Hub architecture, tools, or bindings.\n"
            "DO NOT use memory_dig for casual conversation, greetings, or general AI questions."
        )
        
        # 2. Create or Update the Master Workflow Graph
        # We explicitly use a System Modifier node to inject the Soul into the Agent.
        lollms_graph = {
            "nodes": [
                {"id": 1, "type": "hub/input", "pos":[50, 200], "outputs": [{"name": "Messages", "links": [1]}, {"name": "Settings", "links": [2]}]},
                {"id": 2, "type": "hub/system_modifier", "pos": [450, 50], "inputs":[{"name": "Messages", "link": 1}], "outputs": [{"name": "Updated Messages", "links": [3]}], "properties": {
                    "replace_all": True,
                    "system_prompt": system_prompt
                }},
                {"id": 3, "type": "hub/agent", "pos":[850, 200], "inputs":[{"name": "In Messages", "link": 3}, {"name": "Settings", "link": 2}], "outputs": [{"name": "Final Answer", "links": [4]}], "properties": {
                    "model": "auto", 
                    "max_turns": 10,
                    "memory_system": "lollms" # Points to its own RLM ROM
                }},
                {"id": 4, "type": "hub/output", "pos": [1250, 200], "inputs": [{"name": "Content", "link": 4}]}
            ],
            "links": [[1, 1, 0, 2, 0, "messages"],[2, 1, 1, 3, 1, "object"], [3, 2, 0, 3, 0, "messages"],[4, 3, 0, 4, 0, "string"]]
        }

        existing = await db.execute(select(Workflow).filter_by(name="lollms"))
        wf_obj = existing.scalars().first()
        
        # Inject anti-hallucination skill content into the master soul
        from app.core.skills_manager import SkillsManager
        anti_hal = next((s for s in SkillsManager.get_all_skills() if "hallucination" in s["filename"]), None)
        if anti_hal:
            system_prompt += f"\n\n{anti_hal['raw']}"
            # Update the graph node with the augmented soul
            lollms_graph["nodes"][1]["properties"]["system_prompt"] = system_prompt

        if not wf_obj:
            lollms = Workflow(
                name="lollms",
                description="The Master Architect Workflow. Grounded by Anti-Hallucination protocol.",
                graph_data=lollms_graph,
                workflow_type="master"
            )
            db.add(lollms)
        else:
            # Upgrade existing graph with new augmented soul
            wf_obj.graph_data = lollms_graph
            
        # 3. Seed Recursive Tool Knowledge (ROM)
        rom_seeds =[
            {"t": "HUB_ECOSYSTEM", "c": "The modern LoLLMs ecosystem consists of: 1. LoLLMs Hub (The high-performance gateway/proxy), 2. lollms-client (The Pythonic library for developers), and 3. LoLLMs (The final multi-user application for everyone).", "i": 100},
            {"t": "DEPRECATION_NOTICE", "c": "LoLLMs WebUI is officially deprecated and replaced by LoLLMs Hub Fortress architecture. Never refer to the system as WebUI.", "i": 100},
            {"t": "HUB_PURPOSE", "c": "LoLLMs Hub acts as a 'Fortress' for compute resources, providing enterprise-grade security, multi-user isolation, RAG, and agentic workflows.", "i": 100},
            {"t": "RLM_PROTOCOL", "c": "I use Recursive Language Modeling for memory. High-importance core facts are always in my context. Deep technical engrams are hidden in the ROM and must be retrieved via <memory_dig regex='...'/>.", "i": 95},
            {"t": "HUB_SECURITY", "c": "The Hub enforces strict multi-tenancy. Every user has isolated memories and persistent tool states via the 'lollms' host interface object.", "i": 90},
            {"t": "VLLM_BINDING", "c": "The vLLM binding translates OpenAI-compatible calls to local or remote vLLM clusters, enabling high-throughput inference.", "i": 30},
            {"t": "NOVITA_BINDING", "c": "Novita AI integration provides high-speed cloud inference using the OpenAI protocol branch.", "i": 30},
            {"t": "IRRA_PROTOCOL", "c": "The Intricate Routing & Recovery Algorithm (IRRA) is my core failover engine. It uses intent vectoring to generate a ranked priority queue of candidate models. If a chosen model/server returns a 404 or 503, I automatically trip a global circuit breaker for that node and fail over to the next candidate in the queue without user intervention.", "i": 95},
            {"t": "IRRA_FAILOVER", "c": "IRRA prevents 'Cluster Exhaustion' by dynamically re-routing requests across non-penalized nodes. Each failure during a request lifecycle results in the (Server, Model) pair being blacklisted for that specific turn, evolving the routing path in real-time.", "i": 90},
            {"t": "IRRA_PRIORITY_LOGIC", "c": "In the IRRA hierarchy, numerical priority is inverse: LOWER values indicate HIGHER importance. A model with Priority 1 will always be attempted before a model with Priority 10. This allows administrators to 'pin' preferred hardware or cost-effective models as primary targets.", "i": 95},
            {"t": "UI_CONTROLS", "c": "I can move the user to specific pages using <ui_move_to path='/admin/servers'/> or highlight elements via <ui_highlight selector='#btn-save'/>.", "i": 30}
        ]
        
        for seed in rom_seeds:
            # Check for existence specifically for the 'system' identifier across ANY agent_name
            # This ensures global visibility in the new relaxed query logic.
            exists = await db.execute(
                select(MemoryEntry).filter_by(
                    title=seed["t"], 
                    user_identifier="system"
                )
            )
            if not exists.scalars().first():
                db.add(MemoryEntry(
                    user_identifier="system",
                    agent_name="lollms", # Primary owner
                    category="rom_core" if seed["i"] > 50 else "rom_deep",
                    is_immutable=True,
                    title=seed["t"],
                    content=seed["c"],
                    importance=seed["i"]
                ))
        await db.commit()

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

async def ensure_system_service_key(app: FastAPI) -> None:
    """Ensures a 'store_manager' user and API key exist for internal Hub tasks."""
    from app.crud import apikey_crud
    async with AsyncSessionLocal() as db:
        sys_user = await user_crud.get_user_by_username(db, username="store_manager")
        if not sys_user:
            logger.info("Creating internal 'store_manager' system user...")
            sys_user = await user_crud.create_user(db, UserCreate(username="store_manager", password=secrets.token_urlsafe(32)))
        
        keys = await apikey_crud.get_api_keys_for_user(db, user_id=sys_user.id)
        active_key = next((k for k in keys if k.is_active and not k.is_revoked), None)
        
        if not active_key:
            logger.info("Generating new internal system key...")
            plain, _ = await apikey_crud.create_api_key(db, user_id=sys_user.id, key_name="Internal RAG Service")
            app.state.system_key = plain
        else:
            # Note: We can't recover the plain key for an existing DB entry, 
            # so we generate a fresh one if the state is missing.
            # In a real app, you might store this encrypted in settings.
            # For now, we'll generate a fresh session-based system key if none is in state.
            plain, _ = await apikey_crud.create_api_key(db, user_id=sys_user.id, key_name=f"Session Key {secrets.token_hex(4)}")
            app.state.system_key = plain

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
            # refresh_all_server_models now manages its own session to prevent leaks
            results = await server_crud.refresh_all_server_models()

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
    """
    Lifespan manager that guarantees singleton state across multiple ports.
    Only the first port to boot performs heavy initialization.
    """
    # Fast-path for secondary ports: check if already initialized without locking
    if not shared_state.initialized:
        async with shared_state.init_lock:
            # Double-check inside lock
            if not shared_state.initialized:
                logger.info("--- STARTING PRIMARY HUB INITIALIZATION ---")
                
                # 1. Directories
                for d in ["app/static/uploads", ".ssl", "benchmarks"]:
                    Path(d).mkdir(exist_ok=True)

                # 2. Database & Settings
                try:
                    await init_db()
                    async with AsyncSessionLocal() as db:
                        db_settings_obj = await settings_crud.create_initial_settings(db)
                        shared_state.settings = AppSettingsModel.model_validate(db_settings_obj.settings_data)
                except Exception as db_err:
                    logger.critical(f"FATAL: Database Initialization Failed: {db_err}")
                    raise

                # 3. Network Clients
                timeout = httpx.Timeout(read=None, write=None, connect=5.0, pool=60.0)
                limits = httpx.Limits(max_keepalive_connections=200, max_connections=1000, keepalive_expiry=10.0)
                shared_state.http_client = httpx.AsyncClient(timeout=timeout, limits=limits)
                
                try:
                    s = shared_state.settings
                    cred = f"{s.redis_username}:{s.redis_password}@" if s.redis_username and s.redis_password else (f"{s.redis_username}@" if s.redis_username else "")
                    shared_state.redis = redis.from_url(f"redis://{cred}{s.redis_host}:{s.redis_port}/0", decode_responses=True)
                    await shared_state.redis.ping()
                except:
                    shared_state.redis = None

                # 4. Mandatory Identity Setup
                await create_initial_admin_user()
                await bootstrap_lollms_agent()
                await ensure_system_service_key(app)
                
                # 5. Asset Preparation (Non-critical, don't block boot)
                try:
                    await ensure_local_assets()
                except Exception as asset_err:
                    logger.warning(f"Non-critical assets sync failed: {asset_err}")

                from app.core.bot_manager import BotManager
                shared_state.bot_manager = BotManager(app)

                # --- BOT INFRASTRUCTURE: MOCK REQUEST ---
                class MockRequest:
                    def __init__(self):
                        self.state = type('State', (), {})()
                        self.app = app
                        self.state.enforce_strict_context = False
                        self.state.processing_depth = 0
                        self.state.is_managed_agent = False
                        self.state.source_platform = "Bot"
                        self.url = type('URL', (), {'path': '/api/bot'})()
                        # --- INTERFACE COMPATIBILITY FIX ---
                        self.headers = {}
                        self.method = "POST"
                        self.query_params = {}
                    
                app.state.dummy_request = MockRequest()
                
                # 6. Pre-warm Vectorizer (Heavy Task)
                if shared_state.settings.enable_sb_mra:
                    try:
                        from app.api.v1.routes.proxy import _get_shared_vectorizer
                        shared_state.vectorizer = await _get_shared_vectorizer(shared_state.settings)
                    except Exception as vec_err:
                        logger.warning(f"Vectorizer pre-warm failed: {vec_err}")

                shared_state.initialized = True
                logger.info("--- HUB INITIALIZATION COMPLETE ---")

    # --- PORT-SPECIFIC ASSIGNMENT ---
    # Populate state BEFORE background tasks start
    app.state.settings = shared_state.settings
    app.state.http_client = shared_state.http_client
    app.state.redis = shared_state.redis
    app.state.bot_manager = shared_state.bot_manager

    # Start background services ONLY once
    async with shared_state.init_lock:
        if not shared_state.tasks_started:
             asyncio.create_task(server_crud.refresh_all_server_models())
             app.state.refresh_task = asyncio.create_task(periodic_model_refresh(app))
             asyncio.create_task(shared_state.bot_manager.start_all_active_bots())
             shared_state.tasks_started = True
    
    # Critical for internal routing logic to find the same vectorizer
    if shared_state.vectorizer:
        from app.api.v1.routes import proxy
        proxy._shared_routing_vectorizer = shared_state.vectorizer

    yield

    # Clean up (Only once)
    if shared_state.initialized:
        async with shared_state.init_lock:
            if shared_state.initialized:
                if shared_state.http_client:
                    await shared_state.http_client.aclose()
                if shared_state.redis:
                    await shared_state.redis.close()
                shared_state.initialized = False

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="A secure, high‑performance universal AI gateway and load balancer for Ollama, vLLM, and llama.cpp.",
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
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "img-src 'self' https: data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    response.headers["Content-Security-Policy"] = csp_policy
    return response

app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Standard Routes
app.include_router(health_router, prefix="/api/v1", tags=["Health"])

# Global Route Registration (Required for reliability)
# CRITICAL: Register specific internal APIs BEFORE greedy catch-all proxy routes.
app.include_router(admin_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(playground_chat_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(playground_embedding_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(skills_router, prefix="/api/v1", tags=["Internal API"], include_in_schema=False)
app.include_router(personalities_router, prefix="/api/v1", tags=["Internal API"], include_in_schema=False)
app.include_router(tools_router, prefix="/api/v1", tags=["Internal API"], include_in_schema=False)
app.include_router(importer_router, prefix="/admin/api/importer", tags=["Importer API"], include_in_schema=False)
app.include_router(conception_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(node_builder_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(datastores_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
from app.api.v1.routes.architect import router as arch_router
app.include_router(arch_router, prefix="/admin", tags=["Architect"], include_in_schema=False)

# Protocol Routes
app.include_router(openai_router, prefix="/v1", tags=["OpenAI Protocol"])
app.include_router(proxy_router, prefix="/api", tags=["Ollama Protocol"])

# Secondary UI router inclusions
app.include_router(skills_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(personalities_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(tools_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(importer_router, prefix="/admin/api/importer", tags=["Importer API"], include_in_schema=False)
app.include_router(conception_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(node_builder_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
app.include_router(datastores_router, prefix="/admin", tags=["Admin UI"], include_in_schema=False)
from app.api.v1.routes.architect import router as arch_router
app.include_router(arch_router, prefix="/admin", tags=["Architect"], include_in_schema=False)
from app.api.v1.routes.evaluations import router as eval_router
app.include_router(eval_router, prefix="/admin", tags=["Evaluations"], include_in_schema=False)

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
            print("\n" + "="*60)
            print("🚀 lollms hub is running! 🚀")
            print("="*60)
            print(f"✅ Version: {settings.APP_VERSION}")
            print(f"✅ Mode: {'Production (HTTPS)' if protocol == 'https' else 'Development (HTTP)'}")
            print(f"✅ Listening on port: {port}")
            print("\nTo access the admin dashboard, open your web browser to:")
            print(f"    {protocol}://127.0.0.1:{port}/admin/dashboard")
            print(f"    or {protocol}://localhost:{port}/admin/dashboard")
            print("\nTo stop the server, press CTRL+C in this window.")
            print("="*60 + "\n")
            print("Note: Log messages from 'uvicorn.error' are for general server events and do not necessarily indicate an error.\n")


        # Determine port separation
        app_settings = None
        async with AsyncSessionLocal() as db:
            db_settings_obj = await settings_crud.get_app_settings(db)
            if db_settings_obj:
                app_settings = AppSettingsModel.model_validate(db_settings_obj.settings_data)

        configs = []
        # Primary Port (Dashboard + Configured APIs)
        configs.append(uvicorn.Config("app.main:app", host="0.0.0.0", port=port, 
                                     ssl_keyfile=ssl_keyfile, ssl_certfile=ssl_certfile, log_config=None))

        # Secondary OpenAI Port (If different and enabled)
        if app_settings and app_settings.enable_openai_api and app_settings.openai_port != port:
            logger.info(f"Exposing dedicated OpenAI-compatible listener on port {app_settings.openai_port}")
            configs.append(uvicorn.Config("app.main:app", host="0.0.0.0", port=app_settings.openai_port, 
                                         ssl_keyfile=ssl_keyfile, ssl_certfile=ssl_certfile, log_config=None))

        try:
            servers = [uvicorn.Server(cfg) for cfg in configs]
        except Exception as e:
            logger.error(f"Failed to initialize servers: {e}")
            return

        # Patch first server for banner
        original_startup = servers[0].startup
        async def new_startup(*args, **kwargs):
            await original_startup(*args, **kwargs)
            after_start()
            if app_settings and app_settings.enable_openai_api:
                print(f"✅ OpenAI API:  {protocol}://localhost:{app_settings.openai_port}/v1")
        servers[0].startup = new_startup

        await asyncio.gather(*[s.serve() for s in servers])

    asyncio.run(run_server())
