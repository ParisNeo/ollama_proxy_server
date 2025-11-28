# app/api/v1/routes/admin.py
import logging
from typing import Union, Optional, List, Dict, Any
import redis.asyncio as redis
import psutil
import shutil
import httpx
import asyncio
import secrets
from pathlib import Path
import os
from pydantic import AnyHttpUrl

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.security import verify_password
from app.database.session import get_db
from app.database.models import User
from app.crud import user_crud, apikey_crud, log_crud, server_crud, settings_crud, model_metadata_crud
from app.schema.user import UserCreate
from app.schema.server import ServerCreate, ServerUpdate
from app.schema.settings import AppSettingsModel
from app.api.v1.dependencies import get_csrf_token, validate_csrf_token, login_rate_limiter


logger = logging.getLogger(__name__)
router = APIRouter()

def _safe_int(value: Union[str, int, None], default: int) -> int:
    """Safely convert value to int, returning default if conversion fails"""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    return default

templates = Jinja2Templates(directory="app/templates")

# --- Constants for Logo Upload ---
MAX_LOGO_SIZE_MB = 2
MAX_LOGO_SIZE_BYTES = MAX_LOGO_SIZE_MB * 1024 * 1024
ALLOWED_LOGO_TYPES = ["image/png", "image/jpeg", "image/gif", "image/svg+xml", "image/webp"]
UPLOADS_DIR = Path("app/static/uploads")
SSL_DIR = Path(".ssl")


# --- Sync helper for system info (to be run in threadpool) ---
def get_system_info():
    """Returns a dictionary with system usage information."""
    psutil.cpu_percent(interval=None)
    cpu_percent = psutil.cpu_percent(interval=0.1)
    
    memory = psutil.virtual_memory()
    try:
        disk = shutil.disk_usage('/')
    except FileNotFoundError:
        # Fallback for Windows
        disk = shutil.disk_usage('C:\\')
        
    return {
        "cpu": {"percent": cpu_percent},
        "memory": {
            "total_gb": round(memory.total / (1024**3), 2),
            "used_gb": round(memory.used / (1024**3), 2),
            "percent": memory.percent,
        },
        "disk": {
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "percent": round((disk.used / disk.total) * 100, 2),
        },
    }

# --- Helper for Redis Rate Limit Scan ---
async def get_active_rate_limits(
    redis_client: redis.Redis, 
    db: AsyncSession, 
    settings: AppSettingsModel
) -> List[Dict[str, Any]]:
    if not redis_client:
        return []
        
    limits = []
    # Use SCAN to avoid blocking the server.
    async for key in redis_client.scan_iter("rate_limit:*"):
        try:
            pipe = redis_client.pipeline()
            pipe.get(key)
            pipe.ttl(key)
            results = await pipe.execute()
            count, ttl = results
            
            prefix = key.split(":", 1)[1]

            # Fetch API key details from DB to get the specific rate limit
            api_key = await apikey_crud.get_api_key_by_prefix(db, prefix=prefix)
            
            key_limit = settings.rate_limit_requests
            key_window = settings.rate_limit_window_minutes

            if api_key:
                if api_key.rate_limit_requests is not None:
                    key_limit = api_key.rate_limit_requests
                if api_key.rate_limit_window_minutes is not None:
                    key_window = api_key.rate_limit_window_minutes

            if count is not None and ttl is not None:
                limits.append({
                    "prefix": prefix,
                    "count": int(count),
                    "ttl_seconds": int(ttl),
                    "limit": key_limit,
                    "window_minutes": key_window
                })
        except Exception as e:
            logger.warning(f"Could not parse rate limit key {key}: {e}")
            
    # Sort by the percentage of the limit used
    def sort_key(item):
        if item['limit'] > 0:
            return item['count'] / item['limit']
        return 0
        
    return sorted(limits, key=sort_key, reverse=True)[:10]

# --- Helper to add common context to all templates ---
def get_template_context(request: Request) -> dict:
    return {
        "request": request,
        "is_redis_connected": request.app.state.redis is not None,
        "bootstrap_settings": settings
    }

def flash(request: Request, message: str, category: str = "info"):
    """
    FIX: Re-assign list to session to avoid mutation issues with modern SessionMiddleware.
    """
    messages = request.session.get("_messages", [])
    messages.append({"message": message, "category": category})
    request.session["_messages"] = messages

def get_flashed_messages(request: Request): return request.session.pop("_messages", [])
templates.env.globals["get_flashed_messages"] = get_flashed_messages
async def get_current_user_from_cookie(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    if user_id: 
        user = await user_crud.get_user_by_id(db, user_id=user_id)
        if user:
            db.expunge(user) # Detach the user object from the session to prevent lazy loading errors in templates.
        return user
    return None
async def require_admin_user(request: Request, current_user: Union[User, None] = Depends(get_current_user_from_cookie)) -> User:
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, detail="Not authorized", headers={"Location": str(request.url_for("admin_login"))})
    request.state.user = current_user
    return current_user

@router.get("/api/check-api-keys", name="admin_check_api_keys", response_class=JSONResponse)
async def admin_check_api_keys(
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Check all API keys and their rate limit settings."""
    keys = await apikey_crud.get_all_api_keys(db)
    result = []
    for k in keys:
        result.append({
            "prefix": k.key_prefix,
            "name": k.key_name,
            "rate_limit_requests": k.rate_limit_requests,
            "rate_limit_window_minutes": k.rate_limit_window_minutes,
            "is_active": k.is_active,
            "is_revoked": k.is_revoked
        })
    return JSONResponse(content={"api_keys": result})
    
@router.get("/login", response_class=HTMLResponse, name="admin_login")
async def admin_login_form(request: Request):
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/login.html", context)

@router.post("/login", name="admin_login_post", dependencies=[Depends(login_rate_limiter), Depends(validate_csrf_token)])
async def admin_login_post(request: Request, db: AsyncSession = Depends(get_db), username: str = Form(...), password: str = Form(...)):
    user = await user_crud.get_user_by_username(db, username=username)
    
    is_valid = user and user.is_admin and verify_password(password, user.hashed_password)
    redis_client: redis.Redis = request.app.state.redis
    client_ip = request.client.host

    if not is_valid and redis_client:
        key = f"login_fail:{client_ip}"
        try:
            current_fails = await redis_client.incr(key)
            if current_fails == 1:
                await redis_client.expire(key, 300) 
        except Exception as e:
            logger.error(f"Redis failed during login attempt tracking: {e}")

    if not is_valid:
        flash(request, "Invalid username or password", "error")
        return RedirectResponse(url=request.url_for("admin_login"), status_code=status.HTTP_303_SEE_OTHER)

    if redis_client:
        await redis_client.delete(f"login_fail:{client_ip}")

    request.session["user_id"] = user.id
    flash(request, "Successfully logged in.", "success")
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
    
@router.get("/logout", name="admin_logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=request.url_for("admin_login"), status_code=status.HTTP_303_SEE_OTHER)
    
@router.get("/dashboard", response_class=HTMLResponse, name="admin_dashboard")
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/dashboard.html", context)

# --- API ENDPOINT FOR DYNAMIC DASHBOARD DATA ---
@router.get("/system-info", response_class=JSONResponse, name="admin_system_info")
async def get_system_and_ollama_info(
    request: Request, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user)
):
    http_client: httpx.AsyncClient = request.app.state.http_client
    redis_client: redis.Redis = request.app.state.redis
    app_settings: AppSettingsModel = request.app.state.settings

    # Run blocking psutil calls in a threadpool to avoid blocking the event loop
    system_info_task = run_in_threadpool(get_system_info)
    
    # Fetch active models, server health, and server load concurrently
    running_models_task = server_crud.get_active_models_all_servers(db, http_client)
    server_health_task = server_crud.check_all_servers_health(db, http_client)
    server_load_task = log_crud.get_server_load_stats(db)
    
    # Fetch rate limit info from Redis if available
    rate_limit_task = get_active_rate_limits(redis_client, db, app_settings)
    
    # Await all tasks
    (
        system_info, 
        running_models, 
        server_health, 
        server_load, 
        rate_limits
    ) = await asyncio.gather(
        system_info_task,
        running_models_task,
        server_health_task,
        server_load_task,
        rate_limit_task
    )
    
    # Combine server health and load data into a single structure
    server_load_map = {row.server_name: row.request_count for row in server_load}
    for server in server_health:
        server["request_count"] = server_load_map.get(server["name"], 0)
    
    # Add pricing information to running models
    from app.core.pricing_utils import get_pricing_summary
    import json
    model_pricing_map = {}
    servers = await server_crud.get_servers(db)
    active_servers = [s for s in servers if s.is_active]
    for server in active_servers:
        if server.available_models:
            models_list = server.available_models
            if isinstance(models_list, str):
                try:
                    models_list = json.loads(models_list)
                except:
                    continue
            for model_data in models_list:
                if isinstance(model_data, dict) and "name" in model_data:
                    model_name = model_data["name"]
                    details = model_data.get("details", {})
                    if details.get("pricing"):
                        try:
                            model_pricing_map[model_name] = get_pricing_summary(details["pricing"])
                        except Exception as e:
                            logger.debug(f"Failed to get pricing for {model_name}: {e}")
    
    # Attach pricing to running models
    for model in running_models:
        model_name = model.get("name", "")
        if model_name in model_pricing_map:
            model["pricing"] = model_pricing_map[model_name]
        
    return {
        "system_info": system_info, 
        "running_models": running_models,
        "load_balancer_status": server_health,
        "queue_status": rate_limits
    }
    
@router.get("/stats", response_class=HTMLResponse, name="admin_stats")
async def admin_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    sort_by: str = Query("request_count"),
    sort_order: str = Query("desc"),
):
    context = get_template_context(request)
    key_usage_stats = await log_crud.get_usage_statistics(db, sort_by=sort_by, sort_order=sort_order)
    daily_stats = await log_crud.get_daily_usage_stats(db, days=30)
    hourly_stats = await log_crud.get_hourly_usage_stats(db)
    server_stats = await log_crud.get_server_load_stats(db)
    model_stats = await log_crud.get_model_usage_stats(db)
    context.update({
        "key_usage_stats": key_usage_stats,
        "daily_labels": [row.date.strftime('%Y-%m-%d') for row in daily_stats],
        "daily_data": [row.request_count for row in daily_stats],
        "hourly_labels": [row['hour'] for row in hourly_stats],
        "hourly_data": [row['request_count'] for row in hourly_stats],
        "server_labels": [row.server_name for row in server_stats],
        "server_data": [row.request_count for row in server_stats],
        "model_labels": [row.model_name for row in model_stats],
        "model_data": [row.request_count for row in model_stats],
        "sort_by": sort_by,
        "sort_order": sort_order,
    })
    return templates.TemplateResponse("admin/statistics.html", context)

@router.get("/help", response_class=HTMLResponse, name="admin_help")
async def admin_help_page(request: Request, admin_user: User = Depends(require_admin_user)): 
    return templates.TemplateResponse("admin/help.html", get_template_context(request))

@router.get("/web-search", response_class=HTMLResponse, name="admin_web_search")
async def admin_web_search_page(request: Request, admin_user: User = Depends(require_admin_user)): 
    return templates.TemplateResponse("admin/web_search.html", get_template_context(request))

@router.get("/api/openrouter-credits", response_class=JSONResponse, name="admin_openrouter_credits")
async def get_openrouter_credits_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Get OpenRouter account credits for all OpenRouter servers"""
    from app.core.openrouter_credits import get_openrouter_credits
    from app.core.encryption import decrypt_data
    
    servers = await server_crud.get_servers(db)
    openrouter_servers = [s for s in servers if s.server_type == "openrouter" and s.is_active and s.encrypted_api_key]
    
    credits_info = []
    total_remaining_credits = 0.0
    total_credits_purchased = 0.0
    total_usage = 0.0
    
    logger.info(f"Checking credits for {len(openrouter_servers)} OpenRouter servers")
    
    for server in openrouter_servers:
        try:
            api_key = decrypt_data(server.encrypted_api_key)
            logger.info(f"Server {server.name} - API key decrypted: {api_key[:20] if api_key else 'None'}...")
            if api_key:
                credits_data = await get_openrouter_credits(api_key)
                logger.info(f"Credits data for {server.name}: {credits_data}")
                if credits_data:
                    remaining = float(credits_data.get("remaining_credits", 0))
                    total_purchased = float(credits_data.get("total_credits", 0))
                    usage = float(credits_data.get("total_usage", 0))
                    logger.info(f"Parsed credits for {server.name} - Remaining: {remaining}, Total: {total_purchased}, Used: {usage}")
                    credits_info.append({
                        "server_name": server.name,
                        "total_credits": total_purchased,
                        "total_usage": usage,
                        "remaining_credits": remaining,
                        "refreshed_at": credits_data.get("refreshed_at")
                    })
                    total_remaining_credits += remaining
                    total_credits_purchased += total_purchased
                    total_usage += usage
                else:
                    logger.warning(f"No credits data returned for server {server.name} (API may have returned None)")
        except Exception as e:
            logger.error(f"Failed to get credits for server {server.name}: {e}", exc_info=True)
            continue
    
    # Always return response even if all values are 0 (so frontend can display)
    response_data = {
        "credits_data": credits_info,
        "total_remaining_credits": total_remaining_credits,
        "total_credits_purchased": total_credits_purchased,
        "total_usage": total_usage
    }
    logger.info(f"Returning credits response - Remaining: {total_remaining_credits}, Used: {total_usage}, Total: {total_credits_purchased}")
    return JSONResponse(response_data)

@router.get("/servers", response_class=HTMLResponse, name="admin_servers")
async def admin_server_management(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    context["servers"] = await server_crud.get_servers(db)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/servers.html", context)

@router.post("/servers/add", name="admin_add_server", dependencies=[Depends(validate_csrf_token)])
async def admin_add_server(
    request: Request, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user), 
    server_name: str = Form(...), 
    server_url: str = Form(...), 
    server_type: str = Form(...),
    api_key: Optional[str] = Form(None)
):
    # Auto-set URL for OpenRouter
    if server_type == "openrouter":
        from app.core.openrouter_translator import OPENROUTER_BASE_URL
        server_url = OPENROUTER_BASE_URL
        # Validate API key is provided
        if not api_key or not api_key.strip():
            flash(request, "OpenRouter requires an API key. Please provide one.", "error")
            return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)
    
    existing_server = await server_crud.get_server_by_url(db, url=server_url)
    if existing_server:
        flash(request, f"Server with URL '{server_url}' already exists.", "error")
    else:
        try:
            server_in = ServerCreate(name=server_name, url=server_url, server_type=server_type, api_key=api_key)
            await server_crud.create_server(db, server=server_in)
            flash(request, f"Server '{server_name}' ({server_type}) added successfully.", "success")
        except Exception as e:
            logger.error(f"Error adding server: {e}")
            flash(request, "Invalid URL format or server type.", "error")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/servers/{server_id}/delete", name="admin_delete_server", dependencies=[Depends(validate_csrf_token)])
async def admin_delete_server(request: Request, server_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    await server_crud.delete_server(db, server_id=server_id)
    flash(request, "Server deleted successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/servers/{server_id}/refresh-models", name="admin_refresh_models", dependencies=[Depends(validate_csrf_token)])
async def admin_refresh_models(request: Request, server_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    result = await server_crud.fetch_and_update_models(db, server_id=server_id)
    if result["success"]:
        model_count = len(result["models"])
        flash(request, f"Successfully fetched {model_count} model(s) from server.", "success")
    else:
        flash(request, f"Failed to fetch models: {result['error']}", "error")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)

@router.get("/servers/{server_id}/edit", response_class=HTMLResponse, name="admin_edit_server_form")
async def admin_edit_server_form(request: Request, server_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    server = await server_crud.get_server_by_id(db, server_id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    context = get_template_context(request)
    context["server"] = server
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/edit_server.html", context)

@router.post("/servers/{server_id}/edit", name="admin_edit_server_post", dependencies=[Depends(validate_csrf_token)])
async def admin_edit_server_post(
    request: Request,
    server_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    name: str = Form(...),
    url: str = Form(...),
    server_type: str = Form(...),
    api_key: Optional[str] = Form(None),
    remove_api_key: Optional[bool] = Form(False)
):
    # Auto-set URL for OpenRouter
    if server_type == "openrouter":
        from app.core.openrouter_translator import OPENROUTER_BASE_URL
        url = OPENROUTER_BASE_URL
        # Validate API key is provided (unless we're removing it)
        if remove_api_key:
            flash(request, "OpenRouter requires an API key. Cannot remove it.", "error")
            return RedirectResponse(url=request.url_for("admin_edit_server_form", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
        # Check if we have an existing key or a new one
        existing_server = await server_crud.get_server_by_id(db, server_id)
        if not existing_server:
            raise HTTPException(status_code=404, detail="Server not found")
        has_existing_key = existing_server.encrypted_api_key is not None
        if not has_existing_key and (not api_key or not api_key.strip()):
            flash(request, "OpenRouter requires an API key. Please provide one.", "error")
            return RedirectResponse(url=request.url_for("admin_edit_server_form", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
    
    update_data = {"name": name, "url": url, "server_type": server_type}

    if remove_api_key:
        update_data["api_key"] = ""
    elif api_key is not None and api_key != "":
        update_data["api_key"] = api_key

    server_update = ServerUpdate(**update_data)
    
    updated_server = await server_crud.update_server(db, server_id=server_id, server_update=server_update)
    if not updated_server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    flash(request, f"Server '{name}' updated successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)


# --- NEW SERVER MODEL MANAGEMENT ROUTES ---

@router.get("/servers/{server_id}/manage", response_class=HTMLResponse, name="admin_manage_server_models")
async def admin_manage_server_models(
    request: Request,
    server_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    server = await server_crud.get_server_by_id(db, server_id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    # Get enabled models (default to all if not set)
    # IMPORTANT: The "Manage Models" page should show ALL models from available_models,
    # regardless of enabled_models status. This allows users to enable/disable models.
    # The enabled_models filtering only applies to the main Models Manager page.
    enabled_models_set = set()
    # Check if enabled_models is None (never set) vs empty list (explicitly disabled)
    if server.enabled_models is not None:
        # enabled_models is explicitly set (could be empty list or list with items)
        enabled_models_set = set(server.enabled_models)
    elif server.server_type == "openrouter" and server.available_models:
        # If enabled_models is None (never been set), default to all models being enabled
        import json
        models_list = server.available_models
        if isinstance(models_list, str):
            models_list = json.loads(models_list)
        enabled_models_set = {m.get("name") for m in models_list if isinstance(m, dict) and "name" in m}
    
    # For the "Manage Models" page, we want to show ALL models from available_models
    # so users can enable/disable them. Don't filter by enabled_models here.

    # Parse available_models if it's a JSON string (for template compatibility)
    import json
    parsed_available_models = server.available_models
    if isinstance(parsed_available_models, str):
        try:
            parsed_available_models = json.loads(parsed_available_models)
        except json.JSONDecodeError:
            parsed_available_models = []
    
    # Create a server object with parsed available_models for the template
    # We'll create a simple object that has the same attributes but with parsed models
    class ServerForTemplate:
        def __init__(self, server, available_models):
            self.id = server.id
            self.name = server.name
            self.url = server.url
            self.server_type = server.server_type
            self.is_active = server.is_active
            self.available_models = available_models
            self.enabled_models = server.enabled_models
    
    server_for_template = ServerForTemplate(server, parsed_available_models)

    context = get_template_context(request)
    context["server"] = server_for_template
    context["csrf_token"] = await get_csrf_token(request)
    context["enabled_models"] = enabled_models_set
    return templates.TemplateResponse("admin/manage_server.html", context)

@router.post("/servers/{server_id}/update-enabled-models", name="admin_update_enabled_models", dependencies=[Depends(validate_csrf_token)])
async def admin_update_enabled_models(
    request: Request,
    server_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Update the list of enabled models for an OpenRouter server."""
    server = await server_crud.get_server_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    if server.server_type != "openrouter":
        flash(request, "Model enabling/disabling is only available for OpenRouter servers.", "error")
        return RedirectResponse(url=request.url_for("admin_manage_server_models", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
    
    # Get enabled models from form data
    form_data = await request.form()
    # Form sends "enabled_models" as a list when checkboxes are checked
    enabled_models = form_data.getlist("enabled_models")
    
    # Update the server
    updated_server = await server_crud.update_server_enabled_models(db, server_id, enabled_models)
    if not updated_server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    flash(request, f"Updated enabled models for '{server.name}'. {len(enabled_models)} models enabled.", "success")
    return RedirectResponse(url=request.url_for("admin_manage_server_models", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/servers/{server_id}/pull", name="admin_pull_model", dependencies=[Depends(validate_csrf_token)])
async def admin_pull_model(
    request: Request,
    server_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    model_name: str = Form(...)
):
    server = await server_crud.get_server_by_id(db, server_id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    flash(request, f"Pull initiated for '{model_name}'. This may take several minutes...", "info")
    
    http_client: httpx.AsyncClient = request.app.state.http_client
    result = await server_crud.pull_model_on_server(http_client, server, model_name)

    if result["success"]:
        flash(request, result["message"], "success")
        # Refresh the model list in the proxy's database after a successful pull
        await server_crud.fetch_and_update_models(db, server_id=server_id)
    else:
        flash(request, result["message"], "error")
        
    return RedirectResponse(url=request.url_for("admin_manage_server_models", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/servers/{server_id}/delete-model", name="admin_delete_model", dependencies=[Depends(validate_csrf_token)])
async def admin_delete_model(
    request: Request,
    server_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    model_name: str = Form(...)
):
    server = await server_crud.get_server_by_id(db, server_id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    http_client: httpx.AsyncClient = request.app.state.http_client
    result = await server_crud.delete_model_on_server(http_client, server, model_name)

    if result["success"]:
        flash(request, result["message"], "success")
        # Refresh the model list in the proxy's database after a successful delete
        await server_crud.fetch_and_update_models(db, server_id=server_id)
    else:
        flash(request, result["message"], "error")

    return RedirectResponse(url=request.url_for("admin_manage_server_models", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/servers/{server_id}/load-model", name="admin_load_model", dependencies=[Depends(validate_csrf_token)])
async def admin_load_model(
    request: Request,
    server_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    model_name: str = Form(...)
):
    server = await server_crud.get_server_by_id(db, server_id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    http_client: httpx.AsyncClient = request.app.state.http_client
    result = await server_crud.load_model_on_server(http_client, server, model_name)

    flash(request, result["message"], "success" if result["success"] else "error")
    
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/servers/{server_id}/unload-model", name="admin_unload_model", dependencies=[Depends(validate_csrf_token)])
async def admin_unload_model(
    request: Request,
    server_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    model_name: str = Form(...)
):
    server = await server_crud.get_server_by_id(db, server_id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    http_client: httpx.AsyncClient = request.app.state.http_client
    result = await server_crud.unload_model_on_server(http_client, server, model_name)

    flash(request, result["message"], "success" if result["success"] else "error")
    
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)

# --- NEW: Unload model from Dashboard ---
@router.post("/models/unload", name="admin_unload_model_dashboard", dependencies=[Depends(validate_csrf_token)])
async def admin_unload_model_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    model_name: str = Form(...),
    server_name: str = Form(...)
):
    server = await server_crud.get_server_by_name(db, name=server_name)
    if not server:
        flash(request, f"Server '{server_name}' not found.", "error")
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)

    http_client: httpx.AsyncClient = request.app.state.http_client
    result = await server_crud.unload_model_on_server(http_client, server, model_name)

    flash(request, result["message"], "success" if result["success"] else "error")
    
    await asyncio.sleep(1) # Give backend a moment to update state before reloading
    
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)

# --- MODELS MANAGER ROUTES (NEW) ---
@router.get("/models-manager", response_class=HTMLResponse, name="admin_models_manager")
async def admin_models_manager_page(
    request: Request, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user)
):
    context = get_template_context(request)
    
    # Ensure metadata exists for all discovered models (including OpenRouter)
    all_model_names = await server_crud.get_all_available_model_names(db)
    for model_name in all_model_names:
        await model_metadata_crud.get_or_create_metadata(db, model_name=model_name)
    
    # Get all metadata, but filter to only show models that are currently available
    all_metadata = await model_metadata_crud.get_all_metadata(db)
    available_model_names_set = set(all_model_names)
    
    # Filter metadata to only include models that are currently available on active servers
    available_metadata = [m for m in all_metadata if m.model_name in available_model_names_set]
    
    # Get server info for each model to show which server type it's from
    servers = await server_crud.get_servers(db)
    active_servers = [s for s in servers if s.is_active]
    
    # Build a map of model_name -> (server_type, model_details)
    model_to_server_type = {}
    model_to_details = {}
    for server in active_servers:
        if server.available_models:
            # Handle both list of dicts and JSON string
            models_list = server.available_models
            if isinstance(models_list, str):
                import json
                try:
                    models_list = json.loads(models_list)
                except:
                    continue
            
            for model_data in models_list:
                if isinstance(model_data, dict) and "name" in model_data:
                    model_name = model_data["name"]
                    if model_name not in model_to_server_type:
                        model_to_server_type[model_name] = server.server_type
                        model_to_details[model_name] = model_data.get("details", {})
    
    # Add server type and details info to each metadata item
    # Also format pricing for display
    from app.core.pricing_utils import get_pricing_summary
    for meta in available_metadata:
        meta.server_type = model_to_server_type.get(meta.model_name, "unknown")
        meta.model_details = model_to_details.get(meta.model_name, {})
        # Format pricing if available (for OpenRouter models)
        if meta.model_details.get("pricing"):
            meta.pricing_summary = get_pricing_summary(meta.model_details["pricing"])
        else:
            meta.pricing_summary = None
    
    # Categorize models by server type first, then by category
    from app.core.openrouter_model_sorter import categorize_models_by_server
    categorized_by_server = categorize_models_by_server(available_metadata, model_to_details)
    
    # Create a flat sorted list: Ollama first, then OpenRouter (with free models at top), then vLLM, then others
    sorted_metadata = []
    
    # Sort models within each section by capabilities, CTX, and date
    from app.core.model_sorter import sort_models_by_capabilities, get_model_name
    
    # 1. Ollama models first (cloud models at the very top, then sorted by capabilities)
    if categorized_by_server.get("ollama", {}).get("all"):
        ollama_all = categorized_by_server["ollama"]["all"]
        
        # Separate cloud models from non-cloud models
        cloud_models = []
        non_cloud_models = []
        for model in ollama_all:
            model_name = get_model_name(model)
            if ":cloud" in model_name.lower():
                cloud_models.append(model)
            else:
                non_cloud_models.append(model)
        
        # Sort cloud models first (by capabilities)
        if cloud_models:
            cloud_sorted = sort_models_by_capabilities(cloud_models, model_to_details)
            sorted_metadata.extend(cloud_sorted)
        
        # Then sort non-cloud models (by capabilities)
        if non_cloud_models:
            non_cloud_sorted = sort_models_by_capabilities(non_cloud_models, model_to_details)
            sorted_metadata.extend(non_cloud_sorted)
    
    # 2. OpenRouter models - "Other Models" FIRST (at the very top), then free, then others
    openrouter_cats = categorized_by_server.get("openrouter", {})
    
    # Track which models we've already added to avoid duplicates
    added_model_names = set()
    
    # 2a. "Other Models" FIRST (at the very top of OpenRouter section)
    if openrouter_cats.get("other"):
        other_sorted = sort_models_by_capabilities(openrouter_cats["other"], model_to_details)
        for model in other_sorted:
            model_name = model.model_name if hasattr(model, 'model_name') else str(model)
            if model_name not in added_model_names:
                sorted_metadata.append(model)
                added_model_names.add(model_name)
    
    # 2b. Free models (shown after "Other")
    if openrouter_cats.get("free"):
        free_models = sort_models_by_capabilities(openrouter_cats["free"], model_to_details)
        for model in free_models:
            model_name = model.model_name if hasattr(model, 'model_name') else str(model)
            if model_name not in added_model_names:
                sorted_metadata.append(model)
                added_model_names.add(model_name)
    
    # 2c. Major providers
    if openrouter_cats.get("major_providers"):
        major_sorted = sort_models_by_capabilities(openrouter_cats["major_providers"], model_to_details)
        for model in major_sorted:
            model_name = model.model_name if hasattr(model, 'model_name') else str(model)
            if model_name not in added_model_names:
                sorted_metadata.append(model)
                added_model_names.add(model_name)
    
    # 2d. Uncensored models (only Dolphin-based)
    if openrouter_cats.get("uncensored"):
        uncensored_sorted = sort_models_by_capabilities(openrouter_cats["uncensored"], model_to_details)
        for model in uncensored_sorted:
            model_name = model.model_name if hasattr(model, 'model_name') else str(model)
            if model_name not in added_model_names:
                sorted_metadata.append(model)
                added_model_names.add(model_name)
    
    # 3. vLLM models (sorted by capabilities)
    if categorized_by_server.get("vllm", {}).get("all"):
        vllm_sorted = sort_models_by_capabilities(
            categorized_by_server["vllm"]["all"], 
            model_to_details
        )
        sorted_metadata.extend(vllm_sorted)
    
    # 4. Other models (sorted by capabilities)
    if categorized_by_server.get("other", {}).get("all"):
        other_sorted = sort_models_by_capabilities(
            categorized_by_server["other"]["all"], 
            model_to_details
        )
        sorted_metadata.extend(other_sorted)
    
    context["metadata_list"] = sorted_metadata
    context["categorized_by_server"] = categorized_by_server
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/models_manager.html", context)

@router.post("/models-manager/update", name="admin_update_model_metadata", dependencies=[Depends(validate_csrf_token)])
async def admin_update_model_metadata(
    request: Request, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user)
):
    form_data = await request.form()
    
    # A set to keep track of which models were in the form
    updated_model_ids = set()
    
    # Loop through form data to find metadata fields
    for key, value in form_data.items():
        if key.startswith("description_"):
            meta_id = int(key.split("_")[1])
            updated_model_ids.add(meta_id)
            
    # Now process each model found in the form
    for meta_id in updated_model_ids:
        metadata = await db.get(model_metadata_crud.ModelMetadata, meta_id)
        if metadata:
            update_data = {
                "description": form_data.get(f"description_{meta_id}", "").strip(),
                "supports_images": f"supports_images_{meta_id}" in form_data,
                "is_code_model": f"is_code_model_{meta_id}" in form_data,
                "is_fast_model": f"is_fast_model_{meta_id}" in form_data,
                "supports_tool_calling": f"supports_tool_calling_{meta_id}" in form_data,
                "supports_internet": f"supports_internet_{meta_id}" in form_data,
                "is_thinking_model": f"is_thinking_model_{meta_id}" in form_data,
                "priority": int(form_data.get(f"priority_{meta_id}", 10)),
            }
            await model_metadata_crud.update_metadata(db, model_name=metadata.model_name, **update_data)

    flash(request, "Model metadata updated successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_models_manager"), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/auto-priority-inventory", name="admin_auto_priority_inventory")
async def admin_auto_priority_inventory(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """
    Automatically calculate and update priorities for SELECTED models based on capabilities, price, and descriptions.
    Models are sorted per-provider (Ollama separate from OpenRouter) but can share priority numbers.
    Only models with active endpoints (for OpenRouter) are included.
    """
    from pydantic import BaseModel
    from app.core.openrouter_endpoint_checker import filter_models_with_active_endpoints
    from app.core.encryption import decrypt_data
    import httpx
    
    class InventoryRequest(BaseModel):
        csrf_token: str = None
        selected_models: list[str] = []  # List of model names selected via radio buttons
        mode: str = "free"  # Priority mode: "free", "daily_drive", or "advanced"
    
    try:
        body = await request.json()
        req = InventoryRequest(**body)
        
        # Get all available models and their metadata
        all_model_names = await server_crud.get_all_available_model_names(db)
        all_model_names_set = set(all_model_names)
        all_metadata = await model_metadata_crud.get_all_metadata(db)
        
        # Extract model names first to avoid lazy loading issues
        available_metadata = []
        for m in all_metadata:
            model_name = m.model_name  # Access in async context
            if model_name in all_model_names_set:
                available_metadata.append(m)
        
        # Filter to only selected models (if any were selected)
        if req.selected_models:
            selected_set = set(req.selected_models)
            # Extract model names again to avoid lazy loading
            filtered_metadata = []
            for m in available_metadata:
                model_name = m.model_name  # Access in async context
                if model_name in selected_set:
                    filtered_metadata.append(m)
            available_metadata = filtered_metadata
            logger.info(f"Auto-priority: Analyzing {len(available_metadata)} selected models")
        else:
            logger.info(f"Auto-priority: No models selected, analyzing all {len(available_metadata)} available models")
        
        # Build model details map and server type mapping
        servers = await server_crud.get_servers(db)
        active_servers = [s for s in servers if s.is_active]
        model_details_map = {}
        model_to_server_type = {}  # Map model_name -> server_type
        
        # Separate models by provider
        ollama_models = []
        openrouter_models = []
        openrouter_model_names = []
        openrouter_servers = []
        
        for server in active_servers:
            if server.available_models:
                models_list = server.available_models
                if isinstance(models_list, str):
                    try:
                        models_list = json.loads(models_list)
                    except:
                        continue
                
                for model_data in models_list:
                    if isinstance(model_data, dict) and "name" in model_data:
                        model_name = model_data["name"]
                        model_details_map[model_name] = model_data.get("details", {})
                        # Map model to server type
                        if model_name not in model_to_server_type:
                            model_to_server_type[model_name] = server.server_type
                        
                        # Track OpenRouter models for endpoint checking
                        if server.server_type == "openrouter":
                            openrouter_model_names.append(model_name)
                            if server not in openrouter_servers:
                                openrouter_servers.append(server)
        
        # Filter OpenRouter models to only include those with active endpoints
        http_client: httpx.AsyncClient = request.app.state.http_client
        active_openrouter_models = set()
        
        if openrouter_servers and openrouter_model_names:
            # Get API key from first OpenRouter server
            server = openrouter_servers[0]
            api_key = None
            if server.encrypted_api_key:
                api_key = decrypt_data(server.encrypted_api_key)
            
            if api_key:
                logger.info(f"Checking {len(openrouter_model_names)} OpenRouter models for active endpoints...")
                # Filter to only selected OpenRouter models if selection was made
                models_to_check = [
                    name for name in openrouter_model_names
                    if not req.selected_models or name in req.selected_models
                ]
                active_openrouter_models = await filter_models_with_active_endpoints(
                    models_to_check, api_key, http_client
                )
                logger.info(f"Found {len(active_openrouter_models)} OpenRouter models with active endpoints")
            else:
                logger.warning("No API key found for OpenRouter server, skipping endpoint check")
        
        # Separate models by provider, filtering OpenRouter to active endpoints only
        # Extract model names first to avoid lazy loading issues
        metadata_with_names = []
        for metadata in available_metadata:
            # Access model_name in async context to avoid lazy loading
            model_name = metadata.model_name
            metadata_with_names.append((metadata, model_name))
        
        for metadata, model_name in metadata_with_names:
            # Get server type from mapping (not from metadata attribute)
            server_type = model_to_server_type.get(model_name, "ollama")
            if server_type == "openrouter":
                if model_name in active_openrouter_models:
                    openrouter_models.append(metadata)
                else:
                    logger.debug(f"Excluding {model_name} - no active endpoints")
            else:
                ollama_models.append(metadata)
        
        logger.info(f"Models to analyze: {len(ollama_models)} Ollama, {len(openrouter_models)} OpenRouter (with active endpoints)")
        
        # Calculate priorities based on selected mode
        from app.core.priority_modes import (
            assign_priorities_free_mode,
            assign_priorities_daily_drive_mode,
            assign_priorities_advanced_mode,
            assign_priorities_luxury_mode
        )
        
        mode = req.mode.lower() if req.mode else "free"
        
        if mode == "free":
            priorities = assign_priorities_free_mode(
                ollama_models, openrouter_models, model_details_map
            )
        elif mode == "daily_drive":
            priorities = assign_priorities_daily_drive_mode(
                ollama_models, openrouter_models, model_details_map
            )
        elif mode == "advanced":
            priorities = assign_priorities_advanced_mode(
                ollama_models, openrouter_models, model_details_map
            )
        elif mode == "luxury":
            priorities = assign_priorities_luxury_mode(
                ollama_models, openrouter_models, model_details_map
            )
        else:
            # Fallback to free mode
            logger.warning(f"Unknown mode '{mode}', defaulting to 'free'")
            priorities = assign_priorities_free_mode(
                ollama_models, openrouter_models, model_details_map
            )
        
        # Update priorities in database
        updated_count = 0
        errors = []
        all_selected_models = ollama_models + openrouter_models
        
        # Extract model names and priorities before the update loop to avoid lazy loading
        updates_to_make = []
        for metadata in all_selected_models:
            model_name = metadata.model_name  # Access in async context
            if model_name in priorities:
                new_priority = priorities[model_name]
                current_priority = metadata.priority  # Access in async context
                if current_priority != new_priority:
                    updates_to_make.append((model_name, new_priority))
        
        # Now perform updates
        for model_name, new_priority in updates_to_make:
            try:
                result = await model_metadata_crud.update_metadata(
                    db, 
                    model_name=model_name, 
                    priority=new_priority
                )
                if result:
                    updated_count += 1
                else:
                    errors.append(f"Failed to update {model_name}: metadata not found")
            except Exception as e:
                error_msg = f"Failed to update {model_name}: {str(e)}"
                errors.append(error_msg)
                logger.error(error_msg)
        
        response_data = {
            "success": True,
            "updated_count": updated_count,
            "total_models": len(all_selected_models),
            "ollama_count": len(ollama_models),
            "openrouter_count": len(openrouter_models),
            "mode": mode,
        }
        
        if errors:
            response_data["errors"] = errors[:10]  # Limit to first 10 errors
            response_data["error_count"] = len(errors)
        
        return JSONResponse(response_data)
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Error in auto-priority inventory: {str(e)}\n{error_trace}")
        return JSONResponse({
            "error": f"Error calculating priorities: {str(e)}",
            "details": error_trace.split('\n')[-5:] if len(error_trace) > 100 else error_trace
        }, status_code=500)


@router.post("/api/generate-model-description", name="admin_generate_model_description")
async def admin_generate_model_description(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """Generate a smart AI description for a model using Ollama web search + AI model."""
    from pydantic import BaseModel
    from app.crud import server_crud
    import json
    import os
    import httpx
    
    class DescriptionRequest(BaseModel):
        model_name: str
        ai_model: Optional[str] = None  # AI model to use for generation (from playground)
        csrf_token: str = None
    
    try:
        body = await request.json()
        req = DescriptionRequest(**body)
        
        # Step 1: Determine server type for the model
        servers = await server_crud.get_servers(db)
        active_servers = [s for s in servers if s.is_active]
        model_server_type = "ollama"  # default
        for server in active_servers:
            if server.available_models:
                import json
                models_list = server.available_models
                if isinstance(models_list, str):
                    try:
                        models_list = json.loads(models_list)
                    except:
                        continue
                for model_data in models_list:
                    if isinstance(model_data, dict) and model_data.get("name") == req.model_name:
                        model_server_type = server.server_type
                        break
        
        # Step 2: Scrape OpenRouter's official model page FIRST (if OpenRouter model)
        # This gives us the most accurate, up-to-date capability information
        openrouter_scraped_info = None
        if model_server_type == "openrouter":
            try:
                from app.core.openrouter_scraper import scrape_openrouter_model_info, get_openrouter_capabilities_from_api
                
                # Try API first (faster)
                for server in active_servers:
                    if server.server_type == "openrouter" and server.encrypted_api_key:
                        from app.core.encryption import decrypt_data
                        api_key = decrypt_data(server.encrypted_api_key)
                        if api_key:
                            openrouter_scraped_info = await get_openrouter_capabilities_from_api(req.model_name, api_key)
                            if openrouter_scraped_info:
                                logger.info(f"Got OpenRouter API info for '{req.model_name}': web_search={openrouter_scraped_info.get('supports_web_search')}")
                                break
                
                # Fallback to Playwright scraping if API didn't work
                if not openrouter_scraped_info:
                    openrouter_scraped_info = await scrape_openrouter_model_info(req.model_name, model_server_type)
                    if openrouter_scraped_info:
                        logger.info(f"Scraped OpenRouter page for '{req.model_name}': web_search={openrouter_scraped_info.get('supports_web_search')}")
            except Exception as e:
                logger.warning(f"Failed to scrape OpenRouter for '{req.model_name}': {e}")
        
        # Step 3: Search for current information about the model AND service
        # Search for both the model and the service it runs on
        search_query = f"{req.model_name} AI model capabilities features use cases"
        if model_server_type == "openrouter":
            search_query += " OpenRouter"
        elif model_server_type == "ollama":
            search_query += " Ollama cloud"
        
        search_results = []
        
        try:
            # Use Ollama web search if API keys are configured
            app_settings: AppSettingsModel = request.app.state.settings
            if app_settings.ollama_api_key or app_settings.ollama_api_key_2:
                from app.core.unified_search import UnifiedSearchService
                search_service = UnifiedSearchService(
                    searxng_url=app_settings.searxng_url if app_settings.searxng_url else "http://localhost:7019",
                    ollama_api_key=app_settings.ollama_api_key,
                    ollama_api_key_2=app_settings.ollama_api_key_2,
                    timeout=20.0
                )
                search_response = await search_service.web_search(
                    query=search_query,
                    max_results=20,  # Get at least 20 most recent links for comprehensive analysis
                    engine=None  # Auto: try SearXNG first, fallback to Ollama
                )
                if "results" in search_response:
                    engine_used = search_response.get("engine", "unknown")
                    search_results = [
                        {
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "content": r.get("content", "") or r.get("snippet", "")
                        }
                        for r in search_response["results"]
                    ]
                    if search_results:
                        logger.info(f"Web search ({engine_used}) found {len(search_results)} results for '{req.model_name}'")
                        # Ensure we have at least 20 results if available
                        if len(search_results) < 20 and engine_used == "searxng":
                            logger.debug(f"Only got {len(search_results)} results from SearXNG, but requested 20")
        except Exception as e:
            logger.debug(f"Web search failed: {e}")
        
        if not search_results:
            logger.info(f"Search returned no results for '{req.model_name}', continuing without search context")
        
        # Step 4: Fetch detailed model information from OpenRouter or Ollama API
        from app.core.model_info_fetcher import get_model_info_from_server
        model_info = await get_model_info_from_server(db, req.model_name, server_crud)
        
        # Step 5: Get model metadata to analyze capabilities
        from app.crud import model_metadata_crud
        metadata = await model_metadata_crud.get_metadata_by_model_name(db, req.model_name)
        
        # Build analysis prompt with search results and fetched model info
        capabilities = []
        
        # Initialize variables
        ctx_len = None
        param_size = None
        family = None
        
        # PRIORITY 1: Use scraped OpenRouter info (most accurate, official source)
        if openrouter_scraped_info:
            if openrouter_scraped_info.get("supports_web_search"):
                capabilities.append("web search/internet grounding")
                logger.info(f"OpenRouter scraping confirmed web search support for '{req.model_name}'")
            if openrouter_scraped_info.get("supports_images"):
                capabilities.append("supports images/vision")
            if openrouter_scraped_info.get("supports_tools"):
                capabilities.append("tool calling/function calling")
            if openrouter_scraped_info.get("context_length"):
                ctx_len = openrouter_scraped_info.get("context_length")
            if openrouter_scraped_info.get("description"):
                # Add official description to search context
                search_results.insert(0, {
                    "title": f"OpenRouter Official: {req.model_name}",
                    "url": "https://openrouter.ai/models",
                    "content": openrouter_scraped_info.get("description", "")
                })
        
        # PRIORITY 2: Extract capabilities from fetched model info (API)
        if model_info:
            # OpenRouter capabilities
            if model_info.get("capabilities"):
                capabilities.extend(model_info.get("capabilities", []))
            # Check for vision/multimodal
            if "vision" in str(model_info.get("capabilities", [])).lower() or "image" in str(model_info.get("capabilities", [])).lower():
                if "supports images/vision" not in capabilities:
                    capabilities.append("supports images/vision")
            # Check for web search in supported_parameters
            supported_params = model_info.get("supported_parameters", [])
            if isinstance(supported_params, list) and "web_search_options" in supported_params:
                if "web search/internet grounding" not in capabilities:
                    capabilities.append("web search/internet grounding")
                    logger.info(f"OpenRouter API confirmed web_search_options for '{req.model_name}'")
            # Check context length
            if not ctx_len:
                ctx_len = model_info.get("context_length") or model_info.get("details", {}).get("context_length")
            # Check parameter size
            if not param_size:
                param_size = model_info.get("parameter_size") or model_info.get("details", {}).get("parameter_size")
            # Check family
            if not family:
                family = model_info.get("family") or model_info.get("details", {}).get("family")
        
        # Also check metadata flags
        if metadata and metadata.supports_images:
            if "supports images/vision" not in capabilities:
                capabilities.append("supports images/vision")
        if metadata and metadata.is_code_model:
            if "code generation" not in capabilities:
                capabilities.append("code generation")
        if metadata and metadata.is_fast_model:
            if "fast inference" not in capabilities:
                capabilities.append("fast inference")
        
        # Infer capabilities from model name
        model_name_lower = req.model_name.lower()
        if "thinking" in model_name_lower or "think" in model_name_lower:
            if "thinking/reasoning" not in capabilities:
                capabilities.append("thinking/reasoning")
        if "reasoning" in model_name_lower or "reason" in model_name_lower:
            if "advanced reasoning" not in capabilities:
                capabilities.append("advanced reasoning")
        if "vision" in model_name_lower or "ocr" in model_name_lower:
            if "vision/image processing" not in capabilities:
                capabilities.append("vision/image processing")
        if "code" in model_name_lower or "coder" in model_name_lower:
            if "code generation" not in capabilities:
                capabilities.append("code generation")
        
        # Fallback: try to get context length from server's available_models
        if not ctx_len:
            servers = await server_crud.get_servers(db)
            active_servers = [s for s in servers if s.is_active]
            for server in active_servers:
                if server.available_models:
                    models_list = server.available_models
                    if isinstance(models_list, str):
                        try:
                            models_list = json.loads(models_list)
                        except:
                            continue
                    for model_data in models_list:
                        if isinstance(model_data, dict) and model_data.get("name") == req.model_name:
                            model_details = model_data.get("details", {})
                            ctx_len = model_details.get("context_length")
                            break
        
        # Build comprehensive prompt with search results
        # Use at least 20 most recent links for comprehensive analysis
        search_context = ""
        if search_results:
            search_context = "\n\nRecent information found (using most recent sources):\n"
            # Use all available results (up to 20) for comprehensive analysis
            results_to_use = search_results[:20] if len(search_results) > 20 else search_results
            for i, result in enumerate(results_to_use, 1):
                title = result.get("title", "")
                url = result.get("url", "")
                snippet = result.get("content", "")[:400]  # Longer snippets for better context
                search_context += f"{i}. {title}\n   URL: {url}\n   {snippet}\n\n"
            search_context += f"\nTotal sources analyzed: {len(results_to_use)}\n"
        
        # Add OpenRouter official info to context if available
        official_source_info = ""
        if openrouter_scraped_info:
            official_source_info = "\n\n⚠️ OFFICIAL SOURCE (OpenRouter):\n"
            if openrouter_scraped_info.get("supports_web_search"):
                official_source_info += "✓ Web Search: CONFIRMED (model supports web_search_options parameter)\n"
            else:
                official_source_info += "✗ Web Search: NOT SUPPORTED (not in web_search_options filtered list)\n"
            if openrouter_scraped_info.get("supports_images"):
                official_source_info += "✓ Images/Vision: CONFIRMED\n"
            if openrouter_scraped_info.get("supports_tools"):
                official_source_info += "✓ Tool Calling: CONFIRMED\n"
            if openrouter_scraped_info.get("official_description"):
                official_source_info += f"Official Description: {openrouter_scraped_info.get('official_description')[:200]}\n"
        
        # Build model info context
        model_info_context = f"Model Name: {req.model_name}\n"
        model_info_context += f"Service/Platform: {model_server_type.upper()}\n"
        if ctx_len:
            model_info_context += f"Context Length: {ctx_len:,} tokens\n"
        if param_size:
            model_info_context += f"Parameter Size: {param_size}\n"
        if family:
            model_info_context += f"Model Family: {family}\n"
        if model_info and model_info.get("pricing"):
            pricing = model_info.get("pricing", {})
            if pricing.get("prompt") or pricing.get("completion"):
                # Convert to float to handle string values
                try:
                    prompt_price = float(pricing.get('prompt', 0) or 0)
                    completion_price = float(pricing.get('completion', 0) or 0)
                    model_info_context += f"Pricing: ${prompt_price * 1000000:.2f} / 1M input tokens, ${completion_price * 1000000:.2f} / 1M output tokens\n"
                except (ValueError, TypeError):
                    # If conversion fails, skip pricing info
                    pass
        
        prompt = f"""Analyze this AI model comprehensively and provide:
1. A CONCISE, practical description that highlights what makes this model unique and valuable
2. A COMPLETE JSON object with ALL the model's capabilities (don't focus on just one capability)

{model_info_context}
Capabilities: {', '.join(capabilities) if capabilities else 'Standard language model'}
{official_source_info}
{search_context}

CRITICAL INSTRUCTIONS:
- Check ALL capabilities in the JSON - don't get hung up on just one (like internet grounding)
- Compare this model to similarly sized peers and highlight what sets it apart
- Answer: "Why would a user need this model?" - what's the unique value proposition?
- Highlight differentiators: What makes it better or different from similar models?
- Be comprehensive: Check images, code, speed, tools, internet, thinking - ALL of them

Respond in this EXACT format (JSON first, then description):

```json
{{
  "supports_images": true/false,
  "is_code_model": true/false,
  "is_fast_model": true/false,
  "supports_tool_calling": true/false,
  "supports_internet": true/false,
  "is_thinking_model": true/false
}}
```

Description: [Your description here - MUST be concise, structured, and AI-agent-friendly]

Description format: Write as if communicating with another AI agent. Be concise, structured, and information-dense:
- Use comma-separated keywords/phrases for efficiency
- Include: capabilities, use cases, differentiators, performance notes
- Format: "capability1, capability2, use_case1, use_case2, differentiator1, differentiator2"
- Maximum 2000 characters, but aim for 300-800 for optimal balance
- Focus on what makes this model unique and when to use it

Examples of good AI-agent-friendly descriptions:
- "code generation, semantic analysis, long context, creative writing, excels at coding tasks, rivals specialized code models, high performance for complex applications"
- "science, math, complex reasoning, PhD-level benchmarks, physics, IMO exams, outperforms similar-sized models in mathematical reasoning, high compute thinking steps"
- "multimodal reasoning, vision, code, long context, creative nuance, SOTA tier 1, unique combination of capabilities, best-in-class for complex multi-domain tasks"

CRITICAL: Write concisely like talking to another AI agent. Pack maximum information into minimum words. Use structured format (keywords/phrases separated by commas). This description is used by the auto-router for intelligent model selection.

For the JSON capabilities - CHECK ALL OF THEM, don't focus on just one:
- supports_images: true if model can process images/vision/multimodal
- is_code_model: true if model excels at code generation/editing/programming
- is_fast_model: true if model is optimized for speed/low latency/quick responses
- supports_tool_calling: true if model supports function calling/tools/plugins/APIs
- supports_internet: true if model has web search/real-time data access/grounding. CRITICAL: Check the OFFICIAL SOURCE section above first. If OpenRouter shows "Web Search: CONFIRMED", mark as true. Otherwise, only mark as true if the model EXPLICITLY supports internet/web search capabilities. Many models (including cloud-hosted ones like Ollama cloud) run locally without internet access. Models like Perplexity, Grok, or those explicitly mentioning "web search", "internet grounding", or "real-time data" should be marked true. Do NOT assume flagship models have internet - verify from official sources.
- is_thinking_model: true if model uses thinking/reasoning steps (chain-of-thought, o1/o3 style reasoning). False for "nothink" variants.

REMEMBER: Be comprehensive - check ALL capabilities, not just one. Highlight what makes this model unique and valuable compared to similar models."""

        # Step 4: Use selected AI model (from playground) or default to kimi-k2-1t:cloud
        # Priority: 1) Requested ai_model, 2) kimi-k2-1t:cloud, 3) Any available model
        preferred_model = req.ai_model or "kimi-k2-1t:cloud"
        chosen_server = None
        chosen_model = preferred_model
        
        servers = await server_crud.get_servers(db)
        active_servers = [s for s in servers if s.is_active]
        
        # First, try to find the preferred model (from playground selection or default)
        for server in active_servers:
            if server.available_models:
                models_list = server.available_models
                if isinstance(models_list, str):
                    try:
                        models_list = json.loads(models_list)
                    except:
                        continue
                if not isinstance(models_list, list):
                    continue
                    
                for model_data in models_list:
                    model_name = model_data.get("name") if isinstance(model_data, dict) else str(model_data)
                    if model_name == preferred_model:
                        chosen_server = server
                        chosen_model = preferred_model
                        break
            if chosen_server:
                break
        
        # Fallback: use any available model on any server
        if not chosen_server:
            # Try to find a good model (prefer larger/stronger models for analysis)
            best_model = None
            best_score = 0
            
            for server in active_servers:
                if server.available_models:
                    models_list = server.available_models
                    if isinstance(models_list, str):
                        try:
                            models_list = json.loads(models_list)
                        except:
                            continue
                    if not isinstance(models_list, list):
                        continue
                    
                    for model_data in models_list:
                        if isinstance(model_data, dict):
                            model_name = model_data.get("name")
                            details = model_data.get("details", {})
                            # Score models: prefer larger context, more parameters
                            score = 0
                            ctx_len = details.get("context_length", 0)
                            param_size = details.get("parameter_size_num", 0)
                            if ctx_len > 100000:
                                score += 2
                            elif ctx_len > 32000:
                                score += 1
                            if param_size >= 70:
                                score += 2
                            elif param_size >= 20:
                                score += 1
                            
                            if score > best_score or (score == best_score and not best_model):
                                best_model = model_name
                                best_score = score
                                chosen_server = server
                                chosen_model = model_name
            
            # If still no model, just use the first one we find
            if not chosen_server:
                for server in active_servers:
                    if server.available_models:
                        models_list = server.available_models
                        if isinstance(models_list, str):
                            try:
                                models_list = json.loads(models_list)
                            except:
                                continue
                        if not isinstance(models_list, list):
                            continue
                        if len(models_list) > 0:
                            first_model = models_list[0]
                            chosen_model = first_model.get("name") if isinstance(first_model, dict) else str(first_model)
                            chosen_server = server
                            break
        
        if not chosen_server:
            return JSONResponse({"error": "No active servers or models available. Please ensure at least one server is active and has models."}, status_code=503)
        
        # Create chat completion request
        chat_payload = {
            "model": chosen_model,
            "messages": [
                {"role": "system", "content": "You are an expert AI model analyst. Provide concise, accurate, practical descriptions of AI models based on their capabilities, current information, and real-world use cases. Keep responses to 2-3 sentences maximum."},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
            "options": {
                "temperature": 0.7,
                "top_p": 0.9
            }
        }
        
        # Make request using internal proxy routing
        try:
            from app.core.openrouter_translator import get_openrouter_headers
            from app.core.encryption import decrypt_data
            
            async with httpx.AsyncClient(timeout=90.0, verify=False) as client:
                if chosen_server.server_type == "openrouter":
                    from app.core.openrouter_translator import OPENROUTER_BASE_URL, translate_ollama_to_openrouter_chat
                    url = f"{OPENROUTER_BASE_URL}/chat/completions"
                    
                    # Get API key
                    api_key = None
                    if chosen_server.encrypted_api_key:
                        api_key = decrypt_data(chosen_server.encrypted_api_key)
                    if not api_key:
                        return JSONResponse({"error": "OpenRouter server missing API key"}, status_code=500)
                    
                    headers = get_openrouter_headers(api_key)
                    or_payload = translate_ollama_to_openrouter_chat(chat_payload)
                    try:
                        response = await client.post(url, json=or_payload, headers=headers)
                        response.raise_for_status()
                        result = response.json()
                        description = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    except httpx.HTTPStatusError as e:
                        # If the model doesn't exist, try fallback
                        error_text = ""
                        if e.response:
                            try:
                                error_text = e.response.text.lower() if hasattr(e.response, 'text') else ""
                            except:
                                error_text = ""
                        if e.response and e.response.status_code in (404, 400) and ("no such group" in error_text or "model not found" in error_text):
                            logger.warning(f"Model '{chosen_model}' not available (error: {e.response.text}), trying fallback 'kimi-k2-1t:cloud'")
                            # Try default fallback
                            chat_payload["model"] = "kimi-k2-1t:cloud"
                            or_payload = translate_ollama_to_openrouter_chat(chat_payload)
                            response = await client.post(url, json=or_payload, headers=headers)
                            response.raise_for_status()
                            result = response.json()
                            description = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                        else:
                            raise
                elif chosen_server.server_type == "vllm":
                    from app.core.vllm_translator import translate_ollama_to_vllm_chat
                    url = f"{chosen_server.url.rstrip('/')}/v1/chat/completions"
                    
                    headers = {}
                    if chosen_server.encrypted_api_key:
                        api_key = decrypt_data(chosen_server.encrypted_api_key)
                        if api_key:
                            headers["Authorization"] = f"Bearer {api_key}"
                    
                    vllm_payload = translate_ollama_to_vllm_chat(chat_payload)
                    response = await client.post(url, json=vllm_payload, headers=headers)
                    response.raise_for_status()
                    result = response.json()
                    description = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                else:
                    # Ollama
                    url = f"{chosen_server.url.rstrip('/')}/api/chat"
                    
                    headers = {}
                    if chosen_server.encrypted_api_key:
                        api_key = decrypt_data(chosen_server.encrypted_api_key)
                        if api_key:
                            headers["Authorization"] = f"Bearer {api_key}"
                    
                    # Ensure model is set for Ollama
                    if not chat_payload.get("model"):
                        chat_payload["model"] = chosen_model
                    
                    response = await client.post(url, json=chat_payload, headers=headers)
                    response.raise_for_status()
                    result = response.json()
                    
                    # Ollama response format: {"message": {"content": "..."}}
                    if "message" in result and isinstance(result["message"], dict):
                        description = result.get("message", {}).get("content", "").strip()
                    elif "response" in result:
                        # Some Ollama variants use "response"
                        description = str(result.get("response", "")).strip()
                    else:
                        # Fallback: try to extract from any text field
                        description = str(result).strip()
                        logger.warning(f"Unexpected Ollama response format: {result.keys()}")
            
            # Parse response to extract JSON capabilities and description
            capabilities_json = {}
            cleaned_description = ""
            
            if description:
                # Try to extract JSON from the response
                import re
                json_match = re.search(r'```json\s*(\{.*?\})\s*```', description, re.DOTALL)
                if not json_match:
                    # Try without code block markers
                    json_match = re.search(r'\{[^{}]*"supports_images"[^{}]*\}', description, re.DOTALL)
                
                if json_match:
                    try:
                        capabilities_json = json.loads(json_match.group(1))
                        # Remove JSON from description
                        description = description.replace(json_match.group(0), "").strip()
                    except json.JSONDecodeError:
                        logger.warning("Failed to parse capabilities JSON, will infer from description")
                
                # Extract description (everything after "Description:" or after JSON)
                desc_match = re.search(r'Description:\s*(.+?)(?:\n\n|\Z)', description, re.DOTALL | re.IGNORECASE)
                if desc_match:
                    cleaned_description = desc_match.group(1).strip()
                else:
                    # Use the whole description if no "Description:" marker
                    cleaned_description = description.strip()
                
                # Remove quotes if wrapped
                if (cleaned_description.startswith('"') and cleaned_description.endswith('"')) or (cleaned_description.startswith("'") and cleaned_description.endswith("'")):
                    cleaned_description = cleaned_description[1:-1]
                
                # Remove markdown formatting
                cleaned_description = cleaned_description.replace("**", "").replace("*", "").replace("##", "").replace("#", "").strip()
                
                # Remove common prefixes
                prefixes_to_remove = ["Description:", "The model", "This model", "Model description:"]
                for prefix in prefixes_to_remove:
                    if cleaned_description.lower().startswith(prefix.lower()):
                        cleaned_description = cleaned_description[len(prefix):].strip()
                        if cleaned_description.startswith(":"):
                            cleaned_description = cleaned_description[1:].strip()
                
                # Limit length but allow more space for comprehensive yet concise descriptions
                # Increased to 2000 chars to support detailed but structured AI-agent-friendly descriptions
                if len(cleaned_description) > 2000:
                    cleaned_description = cleaned_description[:1997] + "..."
                
                if len(cleaned_description.strip()) < 10:
                    return JSONResponse({"error": "Generated description too short"}, status_code=500)
                
                # Initialize capabilities_json if not already populated from AI's JSON response
                # The AI's JSON response is the PRIMARY source of truth (it has web search access)
                if not capabilities_json:
                    capabilities_json = {}
                
                # Define all capability fields with defaults
                all_capability_fields = {
                    "supports_images": False,
                    "is_code_model": False,
                    "is_fast_model": False,
                    "supports_tool_calling": False,
                    "supports_internet": False,
                    "is_thinking_model": False
                }
                
                # Only fill in missing fields - never override what the AI explicitly provided
                for field, default_value in all_capability_fields.items():
                    if field not in capabilities_json:
                        capabilities_json[field] = default_value
                
                # Only infer missing capabilities from description if AI didn't provide them
                # This is a fallback - the AI's analysis (with web search) is more accurate
                # IMPORTANT: Only infer if field is completely missing, not if AI explicitly set it to False
                desc_lower = cleaned_description.lower()
                model_lower = req.model_name.lower()
                
                # Infer missing capabilities from description (only if not already set by AI)
                if "supports_images" not in capabilities_json:
                    if any(term in desc_lower or term in model_lower for term in ["vision", "image", "multimodal", "ocr", "visual"]):
                        capabilities_json["supports_images"] = True
                    elif metadata and metadata.supports_images:
                        capabilities_json["supports_images"] = True
                
                if "is_code_model" not in capabilities_json:
                    if any(term in desc_lower or term in model_lower for term in ["code", "coding", "programming", "coder", "developer"]):
                        capabilities_json["is_code_model"] = True
                    elif metadata and metadata.is_code_model:
                        capabilities_json["is_code_model"] = True
                
                if "is_fast_model" not in capabilities_json:
                    if any(term in desc_lower or term in model_lower for term in ["fast", "turbo", "quick", "speed", "low latency"]):
                        capabilities_json["is_fast_model"] = True
                    elif metadata and metadata.is_fast_model:
                        capabilities_json["is_fast_model"] = True
                
                if "supports_tool_calling" not in capabilities_json:
                    if any(term in desc_lower or term in model_lower for term in ["tool", "function", "plugin", "api", "tool calling", "function calling"]):
                        capabilities_json["supports_tool_calling"] = True
                
                # Internet grounding: Only infer if AI didn't explicitly set it (missing, not False)
                # Do NOT assume models have internet - many run locally without web access
                # The AI's web search results are the most accurate source
                if "supports_internet" not in capabilities_json:
                    internet_terms = [
                        "internet", "web", "search", "grounding", "real-time", "live data", 
                        "browser", "web search", "google search", "retrieval", "rag",
                        "web browsing", "online", "web access", "internet access"
                    ]
                    if any(term in desc_lower or term in model_lower for term in internet_terms):
                        capabilities_json["supports_internet"] = True
                    
                    # Only check for models EXPLICITLY designed for web search/grounding
                    explicit_internet_models = ["perplexity", "grok", "you.com", "phind", "brave"]
                    if any(pattern in model_lower for pattern in explicit_internet_models):
                        capabilities_json["supports_internet"] = True
                    
                    # Respect user's manual configuration
                    if metadata and metadata.supports_internet:
                        capabilities_json["supports_internet"] = True
                
                if "is_thinking_model" not in capabilities_json:
                    if any(term in desc_lower or term in model_lower for term in ["thinking", "reasoning", "chain-of-thought", "cot", "step-by-step"]) and "nothink" not in model_lower:
                        capabilities_json["is_thinking_model"] = True
                    elif "nothink" in model_lower:
                        capabilities_json["is_thinking_model"] = False
                
                return JSONResponse({
                    "description": cleaned_description,
                    "capabilities": capabilities_json
                })
            else:
                logger.warning(f"Model {chosen_model} on server {chosen_server.name} returned empty response")
                return JSONResponse({"error": f"Model '{chosen_model}' returned empty response. Try a different model or check server connection."}, status_code=500)
        except httpx.TimeoutException:
            error_msg = f"Request to model '{chosen_model}' timed out after 90 seconds"
            logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=504)
        except httpx.HTTPStatusError as e:
            error_text = e.response.text[:300] if hasattr(e.response, 'text') else str(e)
            error_msg = f"HTTP {e.response.status_code} from {chosen_server.server_type} server: {error_text}"
            logger.error(f"Error calling model: {error_msg}")
            return JSONResponse({"error": f"Model API error (HTTP {e.response.status_code}): {error_text[:200]}"}, status_code=500)
        except httpx.RequestError as e:
            error_msg = f"Network error connecting to {chosen_server.name}: {str(e)}"
            logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=503)
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            error_msg = f"Unexpected error calling model '{chosen_model}': {str(e)}"
            logger.error(f"{error_msg}\n{error_trace}")
            return JSONResponse({"error": error_msg}, status_code=500)
            
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in request: {str(e)}")
        return JSONResponse({"error": "Invalid request format"}, status_code=400)
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Error generating description: {str(e)}\n{error_trace}")
        return JSONResponse({"error": f"Error generating description: {str(e)}"}, status_code=500)


@router.get("/settings", response_class=HTMLResponse, name="admin_settings")
async def admin_settings_form(request: Request, admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    app_settings: AppSettingsModel = request.app.state.settings
    context["settings"] = app_settings
    context["themes"] = app_settings.available_themes # Pass themes to template
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/settings.html", context)


@router.post("/settings", name="admin_settings_post", dependencies=[Depends(validate_csrf_token)])
async def admin_settings_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    logo_file: UploadFile = File(None),
    ssl_key_file: UploadFile = File(None),
    ssl_cert_file: UploadFile = File(None)
):
    current_settings: AppSettingsModel = request.app.state.settings
    form_data = await request.form()
    
    # --- Create a dictionary to hold the final updated values ---
    update_data = {}

    # --- Handle Logo Logic ---
    final_logo_url = current_settings.branding_logo_url
    is_uploaded_logo = final_logo_url and final_logo_url.startswith("/static/uploads/")

    if form_data.get("remove_logo"):
        if is_uploaded_logo:
            logo_to_remove = Path("app" + final_logo_url)
            if logo_to_remove.exists(): os.remove(logo_to_remove)
        final_logo_url = None
        flash(request, "Logo removed successfully.", "success")
    elif logo_file and logo_file.filename:
        # (Validation logic for logo file remains the same)
        file_ext = Path(logo_file.filename).suffix
        secure_filename = f"{secrets.token_hex(16)}{file_ext}"
        save_path = UPLOADS_DIR / secure_filename
        try:
            with open(save_path, "wb") as buffer: shutil.copyfileobj(logo_file.file, buffer)
            if is_uploaded_logo:
                old_logo_path = Path("app" + current_settings.branding_logo_url)
                if old_logo_path.exists(): os.remove(old_logo_path)
            final_logo_url = f"/static/uploads/{secure_filename}"
            flash(request, "New logo uploaded successfully.", "success")
        except Exception as e:
            logger.error(f"Failed to save uploaded logo: {e}")
            flash(request, f"Error saving logo: {e}", "error")
    else:
        final_logo_url = form_data.get("branding_logo_url")
    update_data["branding_logo_url"] = final_logo_url

    # --- Handle SSL File Logic ---
    # Helper function to process SSL file uploads
    async def process_ssl_file(
        file_upload: UploadFile, 
        current_path: Optional[str],
        current_content: Optional[str],
        remove_flag: bool,
        file_type: str # 'key' or 'cert'
    ) -> (Optional[str], Optional[str]):
        
        managed_filename = f"uploaded_{file_type}.pem"
        managed_path = SSL_DIR / managed_filename

        # Priority 1: Removal
        if remove_flag:
            if managed_path.exists():
                os.remove(managed_path)
            flash(request, f"Uploaded SSL {file_type} file removed.", "success")
            return None, None

        # Priority 2: New Upload
        if file_upload and file_upload.filename:
            try:
                content_bytes = await file_upload.read()
                content_str = content_bytes.decode('utf-8')
                with open(managed_path, "w") as f:
                    f.write(content_str)
                flash(request, f"New SSL {file_type} file uploaded successfully.", "success")
                return str(managed_path), content_str
            except Exception as e:
                logger.error(f"Failed to save uploaded SSL {file_type} file: {e}")
                flash(request, f"Error saving SSL {file_type} file: {e}", "error")
                return current_path, current_content # Revert on error

        # Priority 3: Path from form input
        form_path = form_data.get(f"ssl_{file_type}file")
        if form_path != current_path:
             # If a path is specified, it overrides any uploaded file
            if managed_path.exists():
                os.remove(managed_path)
            return form_path, None

        # No changes
        return current_path, current_content

    update_data["ssl_keyfile"], update_data["ssl_keyfile_content"] = await process_ssl_file(
        ssl_key_file, current_settings.ssl_keyfile, current_settings.ssl_keyfile_content,
        bool(form_data.get("remove_ssl_key")), "key"
    )
    update_data["ssl_certfile"], update_data["ssl_certfile_content"] = await process_ssl_file(
        ssl_cert_file, current_settings.ssl_certfile, current_settings.ssl_certfile_content,
        bool(form_data.get("remove_ssl_cert")), "cert"
    )
    
    # --- Update other settings ---
    # (This logic remains the same)
    selected_theme = form_data.get("selected_theme", current_settings.selected_theme)
    ui_style = form_data.get("ui_style", current_settings.ui_style)
    new_redis_password = form_data.get("redis_password")
    
    update_data.update({
        "branding_title": form_data.get("branding_title"),
        "ui_style": ui_style,
        "selected_theme": selected_theme,
        "redis_host": form_data.get("redis_host"),
        "redis_port": _safe_int(form_data.get("redis_port", "6379"), 6379),
        "redis_username": form_data.get("redis_username") or None,
        "model_update_interval_minutes": int(form_data.get("model_update_interval_minutes", 10)),
        "allowed_ips": form_data.get("allowed_ips", ""),
        "denied_ips": form_data.get("denied_ips", ""),
        "blocked_ollama_endpoints": form_data.get("blocked_ollama_endpoints", ""),
        "ollama_api_key": form_data.get("ollama_api_key") or None,
        "ollama_api_key_2": form_data.get("ollama_api_key_2") or None,
        "enable_proxy_web_search": form_data.get("enable_proxy_web_search") == "true",
    })
    if new_redis_password:
        update_data["redis_password"] = new_redis_password
        
    try:
        updated_settings_data = current_settings.model_copy(update=update_data)
        await settings_crud.update_app_settings(db, settings_data=updated_settings_data)
        request.app.state.settings = updated_settings_data
        flash(request, "Settings updated successfully. A restart is required for some changes (like HTTPS) to take effect.", "success")
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid form data for settings: {e}")
        flash(request, "Error: Invalid data provided for a setting (e.g., a port number was not a number).", "error")
    except Exception as e:
        logger.error(f"Failed to update settings: {e}", exc_info=True)
        flash(request, "An unexpected error occurred while saving settings.", "error")

    return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)

# --- USER MANAGEMENT ROUTES ---

@router.get("/users", response_class=HTMLResponse, name="admin_users")
async def admin_user_management(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    sort_by: str = Query("username"),
    sort_order: str = Query("asc"),
):
    context = get_template_context(request)
    context["users"] = await user_crud.get_users(db, sort_by=sort_by, sort_order=sort_order)
    context["csrf_token"] = await get_csrf_token(request)
    context["sort_by"] = sort_by
    context["sort_order"] = sort_order
    return templates.TemplateResponse("admin/users.html", context)

@router.post("/users", name="create_new_user", dependencies=[Depends(validate_csrf_token)])
async def create_new_user(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user), username: str = Form(...), password: str = Form(...)):
    existing_user = await user_crud.get_user_by_username(db, username=username)
    if existing_user:
        flash(request, f"User '{username}' already exists.", "error")
    else:
        user_in = UserCreate(username=username, password=password)
        await user_crud.create_user(db, user=user_in)
        flash(request, f"User '{username}' created successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_users"), status_code=status.HTTP_303_SEE_OTHER)

@router.get("/users/{user_id}/edit", response_class=HTMLResponse, name="admin_edit_user_form")
async def admin_edit_user_form(request: Request, user_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    context = get_template_context(request)
    context["user"] = user
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/edit_user.html", context)

@router.post("/users/{user_id}/edit", name="admin_edit_user_post", dependencies=[Depends(validate_csrf_token)])
async def admin_edit_user_post(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    username: str = Form(...),
    password: Optional[str] = Form(None)
):
    # Check if the new username is already taken by another user
    existing_user = await user_crud.get_user_by_username(db, username=username)
    if existing_user and existing_user.id != user_id:
        flash(request, f"Username '{username}' is already taken.", "error")
        return RedirectResponse(url=request.url_for("admin_edit_user_form", user_id=user_id), status_code=status.HTTP_303_SEE_OTHER)

    updated_user = await user_crud.update_user(db, user_id=user_id, username=username, password=password)
    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    flash(request, f"User '{username}' updated successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_users"), status_code=status.HTTP_303_SEE_OTHER)

@router.get("/users/{user_id}", response_class=HTMLResponse, name="get_user_details")
async def get_user_details(request: Request, user_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")
    context["user"] = user
    context["api_keys"] = await apikey_crud.get_api_keys_for_user(db, user_id=user_id)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/user_details.html", context)

@router.get("/users/{user_id}/stats", response_class=HTMLResponse, name="admin_user_stats")
async def admin_user_stats(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
):
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    context = get_template_context(request)
    
    daily_stats = await log_crud.get_daily_usage_stats_for_user(db, user_id=user_id, days=30)
    hourly_stats = await log_crud.get_hourly_usage_stats_for_user(db, user_id=user_id)
    server_stats = await log_crud.get_server_load_stats_for_user(db, user_id=user_id)
    model_stats = await log_crud.get_model_usage_stats_for_user(db, user_id=user_id)

    context.update({
        "user": user,
        "daily_labels": [row.date.strftime('%Y-%m-%d') for row in daily_stats],
        "daily_data": [row.request_count for row in daily_stats],
        "hourly_labels": [row['hour'] for row in hourly_stats],
        "hourly_data": [row['request_count'] for row in hourly_stats],
        "server_labels": [row.server_name for row in server_stats if row.server_name],
        "server_data": [row.request_count for row in server_stats if row.server_name],
        "model_labels": [row.model_name for row in model_stats],
        "model_data": [row.request_count for row in model_stats],
    })
    
    # Create a new template for this
    return templates.TemplateResponse("admin/user_statistics.html", context)

@router.post("/users/{user_id}/keys/create", name="admin_create_key", dependencies=[Depends(validate_csrf_token)])
async def create_user_api_key(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    key_name: str = Form(...),
    rate_limit_requests: Optional[int] = Form(None),
    rate_limit_window_minutes: Optional[int] = Form(None),
):
    # --- FIX: Check for existing key with the same name for this user ---
    existing_key = await apikey_crud.get_api_key_by_name_and_user_id(db, key_name=key_name, user_id=user_id)
    if existing_key:
        flash(request, f"An API key with the name '{key_name}' already exists for this user.", "error")
        return RedirectResponse(url=request.url_for("get_user_details", user_id=user_id), status_code=status.HTTP_303_SEE_OTHER)
    # --- END FIX ---

    current_admin_id = admin_user.id
    
    plain_key, _ = await apikey_crud.create_api_key(
        db, 
        user_id=user_id, 
        key_name=key_name,
        rate_limit_requests=rate_limit_requests,
        rate_limit_window_minutes=rate_limit_window_minutes
    )
    
    context = get_template_context(request)
    context["plain_key"] = plain_key
    context["user_id"] = user_id
    request.state.user = await db.get(User, current_admin_id) # For base template
    return templates.TemplateResponse("admin/key_created.html", context)

@router.post("/keys/{key_id}/toggle-active", name="admin_toggle_key_active", dependencies=[Depends(validate_csrf_token)])
async def toggle_key_active_status(
    request: Request,
    key_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
):
    key = await apikey_crud.toggle_api_key_active(db, key_id=key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API Key not found or already revoked")
    
    new_status = "enabled" if key.is_active else "disabled"
    flash(request, f"API Key '{key.key_name}' has been {new_status}.", "success")
    return RedirectResponse(url=request.url_for("get_user_details", user_id=key.user_id), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/keys/{key_id}/revoke", name="admin_revoke_key", dependencies=[Depends(validate_csrf_token)])
async def revoke_user_api_key(
    request: Request,
    key_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
):
    key = await apikey_crud.get_api_key_by_id(db, key_id=key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API Key not found")
    
    await apikey_crud.revoke_api_key(db, key_id=key_id)
    flash(request, f"API Key '{key.key_name}' has been revoked.", "success")
    return RedirectResponse(url=request.url_for("get_user_details", user_id=key.user_id), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/users/{user_id}/delete", name="delete_user_account", dependencies=[Depends(validate_csrf_token)])
async def delete_user_account(request: Request, user_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        flash(request, "Cannot delete an admin account.", "error")
        return RedirectResponse(url=request.url_for("admin_users"), status_code=status.HTTP_303_SEE_OTHER)
    
    await user_crud.delete_user(db, user_id=user_id)
    flash(request, f"User '{user.username}' has been deleted.", "success")
    return RedirectResponse(url=request.url_for("admin_users"), status_code=status.HTTP_303_SEE_OTHER)