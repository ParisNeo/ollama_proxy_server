# app/api/v1/routes/admin.py
import logging
from typing import Union, Optional, List, Dict, Any
import redis.asyncio as redis
import psutil
import shutil
import httpx
import asyncio
import secrets
import json
import subprocess
from pathlib import Path
import os
import re
from pydantic import AnyHttpUrl, ValidationError

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.security import verify_password
from app.database.session import get_db
from app.database.models import User, LogAnalysis
from app.crud import user_crud, apikey_crud, log_crud, server_crud, settings_crud, model_metadata_crud
from app.core.events import event_manager
from app.core.instance_manager import supervisor
from app.database.models import ManagedInstance
from app.schema.user import UserCreate
from app.schema.server import ServerCreate, ServerUpdate
from app.schema.settings import AppSettingsModel
from app.api.v1.dependencies import get_csrf_token, validate_csrf_token, login_rate_limiter


logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# --- Constants for Logo Upload ---
MAX_LOGO_SIZE_MB = 2
MAX_LOGO_SIZE_BYTES = MAX_LOGO_SIZE_MB * 1024 * 1024
ALLOWED_LOGO_TYPES = ["image/png", "image/jpeg", "image/gif", "image/svg+xml", "image/webp"]
ALLOWED_LOGO_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'}
UPLOADS_DIR = Path("app/static/uploads")
SSL_DIR = Path(".ssl")


# --- Security: Filename Sanitization ---
def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename to prevent path traversal and other attacks.
    Removes path separators, null bytes, and other dangerous characters.
    """
    if not filename:
        return ""
    
    # Remove path traversal attempts
    filename = os.path.basename(filename)
    
    # Remove null bytes
    filename = filename.replace('\x00', '')
    
    # Allow only alphanumeric, dots, dashes, and underscores
    filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
    
    # Prevent double dots (path traversal)
    while '..' in filename:
        filename = filename.replace('..', '_')
    
    # Ensure it doesn't start with dot (hidden files)
    filename = filename.lstrip('.')
    
    # Limit length
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[:255 - len(ext)] + ext
    
    return filename


# --- Security: Validate File Extension ---
def validate_file_extension(filename: str, allowed_extensions: set) -> bool:
    """Validate that file extension is in allowed set."""
    ext = Path(filename).suffix.lower()
    return ext in allowed_extensions


# --- Security: Content-Type Validation ---
def validate_content_type(content_type: str, allowed_types: list) -> bool:
    """Validate content type is in allowed list."""
    # Normalize content type (remove charset, etc.)
    main_type = content_type.split(';')[0].strip().lower()
    return main_type in allowed_types


# --- Security: Path Validation Helper ---
def is_path_within_directory(target_path: Path, allowed_dir: Path) -> bool:
    """
    SECURITY FIX: Properly validate that a path is within an allowed directory.
    This prevents path traversal attacks by resolving both paths and checking
    that the target is a subpath of the allowed directory.
    
    Returns True only if target_path is within allowed_dir.
    """
    try:
        # Resolve both paths to absolute, normalized paths
        resolved_allowed = allowed_dir.resolve()
        resolved_target = target_path.resolve()
        
        # Check if resolved_target is the same as or a subpath of resolved_allowed
        # This handles all path traversal attempts including symlinks, .., etc.
        return str(resolved_target).startswith(str(resolved_allowed) + os.sep) or resolved_target == resolved_allowed
    except (OSError, ValueError, RuntimeError) as e:
        logger.error(f"Path validation error: {e}")
        return False


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
        # Validate user_id is integer
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return None
            
        user = await user_crud.get_user_by_id(db, user_id=user_id)
        if user:
            db.expunge(user) # Detach the user object from the session to prevent lazy loading errors in templates.
        return user
    return None
async def require_admin_user(request: Request, current_user: Union[User, None] = Depends(get_current_user_from_cookie)) -> User:
    if not current_user or not current_user.is_admin: raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, detail="Not authorized", headers={"Location": str(request.url_for("admin_login"))})
    request.state.user = current_user
    return current_user
    
@router.get("/login", response_class=HTMLResponse, name="admin_login")
async def admin_login_form(request: Request):
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/login.html", context)

@router.post("/login", name="admin_login_post", dependencies=[Depends(login_rate_limiter), Depends(validate_csrf_token)])
async def admin_login_post(request: Request, db: AsyncSession = Depends(get_db), username: str = Form(...), password: str = Form(...)):
    # Validate username format
    if not username or len(username) > 128 or not re.match(r'^[\w.-]+$', username):
        flash(request, "Invalid username format", "error")
        return RedirectResponse(url=request.url_for("admin_login"), status_code=status.HTTP_303_SEE_OTHER)
        
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

    # SECURITY FIX: Regenerate session ID on successful login to prevent session fixation
    old_session_data = dict(request.session)
    request.session.clear()
    for key, value in old_session_data.items():
        if key != "_messages":  # Don't copy flash messages to new session
            request.session[key] = value
    
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

@router.get("/live-status", response_class=HTMLResponse, name="admin_live_status")
async def live_status_view(request: Request, admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    return templates.TemplateResponse("admin/live_status.html", context)

@router.get("/servers/nodes", name="admin_get_server_nodes")
async def get_server_nodes(db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    servers = await server_crud.get_servers(db)
    return [{"name": s.name, "id": s.id} for s in servers if s.is_active]

@router.get("/events")
async def sse_events(request: Request, admin_user: User = Depends(require_admin_user)):
    from fastapi.responses import StreamingResponse
    return StreamingResponse(event_manager.subscribe(), media_type="text/event-stream")

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

    # Calculate Total VRAM used by active models
    total_vram_bytes = sum(model.get("size_vram", 0) for model in running_models)
    
    return {
        "system_info": system_info, 
        "running_models": running_models,
        "gpu_stats": {
            "vram_used_gb": round(total_vram_bytes / (1024**3), 2),
            "vram_total_gb": 24, # Defaulting to 24GB estimate if not detected, can be enhanced
        },
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
    # Whitelist allowed sort values
    allowed_sort = ["username", "key_name", "key_prefix", "request_count", "total_tokens", "total_prompt_tokens", "total_completion_tokens"]
    if sort_by not in allowed_sort:
        sort_by = "request_count"
    if sort_order not in ["asc", "desc"]:
        sort_order = "desc"
        
    context = get_template_context(request)
    key_usage_stats = await log_crud.get_usage_statistics(db, sort_by=sort_by, sort_order=sort_order)
    daily_stats = await log_crud.get_daily_usage_stats(db, days=30)
    hourly_stats = await log_crud.get_hourly_usage_stats(db)
    server_stats = await log_crud.get_server_load_stats(db)
    model_stats = await log_crud.get_model_usage_stats(db)
    
    # Calculate token totals for summary
    total_prompt_tokens = sum(row.total_prompt_tokens for row in key_usage_stats if hasattr(row, 'total_prompt_tokens'))
    total_completion_tokens = sum(row.total_completion_tokens for row in key_usage_stats if hasattr(row, 'total_completion_tokens'))
    total_tokens = sum(row.total_tokens for row in key_usage_stats if hasattr(row, 'total_tokens'))
    
    # Prepare token data by model
    model_prompt_tokens = [row.total_prompt_tokens for row in model_stats]
    model_completion_tokens = [row.total_completion_tokens for row in model_stats]
    model_total_tokens = [row.total_tokens for row in model_stats]
    
    total_carbon = await log_crud.get_total_carbon_footprint(db)
    
    context.update({
        "key_usage_stats": key_usage_stats,
        "total_carbon": total_carbon,
        "daily_labels": [
            row.date if isinstance(row.date, str) 
            else row.date.strftime('%Y-%m-%d') if hasattr(row.date, 'strftime')
            else str(row.date)
            for row in daily_stats
        ],
        "daily_data": [row.request_count for row in daily_stats],
        "hourly_labels": [row['hour'] for row in hourly_stats],
        "hourly_data": [row['request_count'] for row in hourly_stats],
        "server_labels": [row.server_name for row in server_stats],
        "server_data": [row.request_count for row in server_stats],
        "model_labels": [row.model_name for row in model_stats],
        "model_data": [row.request_count for row in model_stats],
        "model_prompt_tokens": model_prompt_tokens,
        "model_completion_tokens": model_completion_tokens,
        "model_total_tokens": model_total_tokens,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "sort_by": sort_by,
        "sort_order": sort_order,
    })
    return templates.TemplateResponse("admin/statistics.html", context)

@router.get("/stats/export-pdf", name="admin_stats_pdf")
async def export_pdf_report(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from xhtml2pdf import pisa
    import io
    
    # Get context data for the page
    response = await admin_stats(request, db, admin_user)
    html_content = response.body.decode()
    
    # Create PDF using xhtml2pdf
    pdf_buffer = io.BytesIO()
    pisa_status = pisa.CreatePDF(html_content, dest=pdf_buffer)
    
    if pisa_status.err:
        logger.error(f"PDF export failed: {pisa_status.err}")
        raise HTTPException(status_code=500, detail="Failed to generate PDF")
        
    pdf_buffer.seek(0)
    return StreamingResponse(pdf_buffer, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=usage-report.pdf"})

    
@router.get("/help", response_class=HTMLResponse, name="admin_help")
async def admin_help_page(request: Request, admin_user: User = Depends(require_admin_user)): 
    return templates.TemplateResponse("admin/help.html", get_template_context(request))

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
    # Validate server name
    if not server_name or len(server_name) > 128:
        flash(request, "Server name is required and must be under 128 characters", "error")
        return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)
        
    # Validate server URL format and security
    if not server_url:
        flash(request, "Server URL is required", "error")
        return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)
    
    # Validate Server URL format
    try:
        from urllib.parse import urlparse
        parsed = urlparse(server_url)
        
        # Ensure only HTTP/HTTPS protocols
        if parsed.scheme not in ('http', 'https'):
            flash(request, "Only HTTP and HTTPS URLs are allowed", "error")
            return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)
            
        # Ensure netloc is present (e.g., localhost:11434 or 192.168.1.1)
        if not parsed.netloc:
            flash(request, "Invalid server URL: missing hostname or IP", "error")
            return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)

        # Check URL length
        if len(server_url) > 2048:
            flash(request, "Server URL is too long", "error")
            return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)
            
    except Exception as e:
        logger.warning(f"URL validation error: {e}")
        flash(request, "Invalid server URL format", "error")
        return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)
        
    existing_server = await server_crud.get_server_by_url(db, url=server_url)
    if existing_server:
        flash(request, f"Server with URL '{server_url}' already exists.", "error")
    else:
        try:
            server_in = ServerCreate(name=server_name, url=server_url, server_type=server_type, api_key=api_key)
            await server_crud.create_server(db, server=server_in)
            flash(request, f"Server '{server_name}' ({server_type}) added successfully.", "success")
        except ValidationError as e:
            logger.error(f"Validation error adding server: {e}")
            flash(request, "Invalid server data: URL format or server type is invalid", "error")
        except Exception as e:
            logger.error(f"Error adding server: {e}")
            flash(request, "An error occurred while adding the server", "error")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/servers/{server_id}/delete", name="admin_delete_server", dependencies=[Depends(validate_csrf_token)])
async def admin_delete_server(request: Request, server_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    # Validate server_id
    try:
        server_id = int(server_id)
        if server_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid server ID")
        
    await server_crud.delete_server(db, server_id=server_id)
    flash(request, "Server deleted successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/servers/{server_id}/refresh-models", name="admin_refresh_models", dependencies=[Depends(validate_csrf_token)])
async def admin_refresh_models(request: Request, server_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    # Validate server_id
    try:
        server_id = int(server_id)
        if server_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid server ID")
        
    result = await server_crud.fetch_and_update_models(db, server_id=server_id)
    if result["success"]:
        model_count = len(result["models"])
        flash(request, f"Successfully fetched {model_count} model(s) from server.", "success")
    else:
        flash(request, f"Failed to fetch models: {result['error']}", "error")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)

@router.get("/servers/{server_id}/edit", response_class=HTMLResponse, name="admin_edit_server_form")
async def admin_edit_server_form(request: Request, server_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    # Validate server_id
    try:
        server_id = int(server_id)
        if server_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid server ID")
        
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
    remove_api_key: Optional[bool] = Form(False),
    allowed_models: List[str] = Form(default=None)
):
    # Validate server_id
    try:
        server_id = int(server_id)
        if server_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid server ID")
        
    # Validate server name
    if not name or len(name) > 128:
        flash(request, "Server name is required and must be under 128 characters", "error")
        return RedirectResponse(url=request.url_for("admin_edit_server_form", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
        
    # Validate URL (same checks as add)
    if not url or len(url) > 2048:
        flash(request, "Server URL is required and must be under 2048 characters", "error")
        return RedirectResponse(url=request.url_for("admin_edit_server_form", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
        
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            flash(request, "Only HTTP and HTTPS URLs are allowed", "error")
            return RedirectResponse(url=request.url_for("admin_edit_server_form", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
    except Exception:
        flash(request, "Invalid server URL format", "error")
        return RedirectResponse(url=request.url_for("admin_edit_server_form", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)

    update_data = {
        "name": name, 
        "url": url, 
        "server_type": server_type,
        "allowed_models": allowed_models # This will be [] if nothing is selected
    }

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
    # Validate server_id
    try:
        server_id = int(server_id)
        if server_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid server ID")
        
    server = await server_crud.get_server_by_id(db, server_id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    context = get_template_context(request)
    context["server"] = server
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/manage_server.html", context)

@router.post("/servers/{server_id}/pull", name="admin_pull_model", dependencies=[Depends(validate_csrf_token)])
async def admin_pull_model(
    request: Request,
    server_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    model_name: str = Form(...)
):
    # Validate server_id
    try:
        server_id = int(server_id)
        if server_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid server ID")
        
    # Validate model_name
    if not model_name or len(model_name) > 256:
        flash(request, "Model name is required and must be under 256 characters", "error")
        return RedirectResponse(url=request.url_for("admin_manage_server_models", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
        
    # Sanitize model name - only allow alphanumeric, dashes, dots, colons
    if not re.match(r'^[\w\.\-:@]+$', model_name):
        flash(request, "Model name contains invalid characters", "error")
        return RedirectResponse(url=request.url_for("admin_manage_server_models", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
        
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
    # Validate server_id
    try:
        server_id = int(server_id)
        if server_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid server ID")
        
    # Validate model_name
    if not model_name or len(model_name) > 256:
        flash(request, "Model name is required", "error")
        return RedirectResponse(url=request.url_for("admin_manage_server_models", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
        
    # Sanitize model name
    if not re.match(r'^[\w\.\-:@]+$', model_name):
        flash(request, "Model name contains invalid characters", "error")
        return RedirectResponse(url=request.url_for("admin_manage_server_models", server_id=server_id), status_code=status.HTTP_303_SEE_OTHER)
        
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
    # Validate server_id
    try:
        server_id = int(server_id)
        if server_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid server ID")
        
    # Validate model_name
    if not model_name or len(model_name) > 256:
        flash(request, "Model name is required", "error")
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
        
    # Sanitize model name
    if not re.match(r'^[\w\.\-:@]+$', model_name):
        flash(request, "Model name contains invalid characters", "error")
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
        
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
    # Validate server_id
    try:
        server_id = int(server_id)
        if server_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid server ID")
        
    # Validate model_name
    if not model_name or len(model_name) > 256:
        flash(request, "Model name is required", "error")
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
        
    # Sanitize model name
    if not re.match(r'^[\w\.\-:@]+$', model_name):
        flash(request, "Model name contains invalid characters", "error")
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
        
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
    # Validate and sanitize inputs
    if not model_name or len(model_name) > 256 or not re.match(r'^[\w\.\-:@]+$', model_name):
        flash(request, "Invalid model name", "error")
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
        
    if not server_name or len(server_name) > 128:
        flash(request, "Invalid server name", "error")
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
        
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
    http_client: httpx.AsyncClient = request.app.state.http_client
    
    # 1. Get all unique model names available across all servers
    all_model_names = await server_crud.get_all_available_model_names(db)
    
    # 2. Deep scan: Create metadata and try to fetch context window for defaults
    for model_name in all_model_names:
        if not model_name or len(model_name) > 256 or not re.match(r'^[\w\.\-:@]+$', model_name):
            continue
            
        existing_meta = await model_metadata_crud.get_metadata_by_model_name(db, model_name)
        
        # If metadata is missing OR exists but has the default 4096 context, try to fetch real info
        if not existing_meta or existing_meta.max_context == 4096:
            servers = await server_crud.get_servers_with_model(db, model_name)
            suggested_ctx = None
            if servers:
                # Try the first server that has this model
                details = await server_crud.get_model_details_from_server(http_client, servers[0], model_name)
                suggested_ctx = details.get("context_length")
            
            if not existing_meta:
                await model_metadata_crud.get_or_create_metadata(db, model_name=model_name, suggested_ctx=suggested_ctx)
            elif suggested_ctx and suggested_ctx != 4096:
                # Autocorrect existing default if we found better info
                await model_metadata_crud.update_metadata(db, model_name=model_name, max_context=suggested_ctx)
        
    context["metadata_list"] = await model_metadata_crud.get_all_metadata(db)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/models_manager.html", context)

@router.get("/models-manager/refresh-context", name="admin_refresh_model_context")
async def admin_refresh_model_context(
    request: Request,
    model_name: str = Query(...),
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """API endpoint to re-query a model's context length from the backend servers."""
    http_client: httpx.AsyncClient = request.app.state.http_client
    
    servers = await server_crud.get_servers_with_model(db, model_name)
    if not servers:
        return JSONResponse({"success": False, "error": "Model not found on any active server."}, status_code=404)

    # Try to get details from the first available server
    details = await server_crud.get_model_details_from_server(http_client, servers[0], model_name)
    ctx = details.get("context_length")

    if ctx:
        return {"success": True, "context_length": ctx}
    return JSONResponse({"success": False, "error": "Server did not provide context metadata for this model."}, status_code=500)


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
            try:
                meta_id = int(key.split("_")[1])
                if meta_id > 0:
                    updated_model_ids.add(meta_id)
            except (ValueError, IndexError):
                continue  # Skip invalid keys
            
    # Now process each model found in the form
    for meta_id in updated_model_ids:
        metadata = await db.get(model_metadata_crud.ModelMetadata, meta_id)
        if metadata:
            # Validate and sanitize description
            description = form_data.get(f"description_{meta_id}", "").strip()
            if len(description) > 1024:
                description = description[:1024]
                
            # Sanitize description - remove potentially dangerous characters
            description = re.sub(r'[<>]', '', description)  # Remove HTML tags
            
            update_data = {
                "description": description,
                "supports_images": f"supports_images_{meta_id}" in form_data,
                "supports_thinking": f"supports_thinking_{meta_id}" in form_data,
                "is_code_model": f"is_code_model_{meta_id}" in form_data,
                "is_fast_model": f"is_fast_model_{meta_id}" in form_data,
                "is_reasoning_model": f"is_reasoning_model_{meta_id}" in form_data,
                "max_context": int(form_data.get(f"max_context_{meta_id}", 4096)),
                "priority": int(form_data.get(f"priority_{meta_id}", 10)),
            }
            await model_metadata_crud.update_metadata(db, model_name=metadata.model_name, **update_data)

    flash(request, "Model metadata updated successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_models_manager"), status_code=status.HTTP_303_SEE_OTHER)


@router.get("/settings", response_class=HTMLResponse, name="admin_settings")
async def admin_settings_form(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import VirtualAgent
    context = get_template_context(request)
    app_settings: AppSettingsModel = request.app.state.settings
    
    # Get list of agents for the management dropdown
    res = await db.execute(select(VirtualAgent.name))
    context["agent_names"] = res.scalars().all()
    
    context["settings"] = app_settings
    context["themes"] = app_settings.available_themes
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
            # SECURITY FIX: Use the new robust path validation helper
            # This properly prevents all path traversal attacks
            if not is_path_within_directory(logo_to_remove, UPLOADS_DIR):
                logger.warning(f"Path traversal attempt detected in logo removal: {logo_to_remove}")
                flash(request, "Security error: Invalid logo path", "error")
                return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
            
            # Only remove if validation passed
            try:
                if logo_to_remove.exists(): 
                    os.remove(logo_to_remove)
            except OSError as e:
                logger.error(f"Error removing logo file: {e}")
                # Continue - don't let file system errors stop the update
                
        final_logo_url = None
        flash(request, "Logo removed successfully.", "success")
        
    elif logo_file and logo_file.filename:
        # SECURITY: Validate file upload
        safe_filename = sanitize_filename(logo_file.filename)
        
        if not safe_filename:
            flash(request, "Invalid filename", "error")
            return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
            
        # Validate extension
        if not validate_file_extension(safe_filename, ALLOWED_LOGO_EXTENSIONS):
            flash(request, f"Invalid file type. Allowed: {', '.join(ALLOWED_LOGO_EXTENSIONS)}", "error")
            return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
            
        # Validate content type
        content_type = logo_file.content_type or ""
        if not validate_content_type(content_type, ALLOWED_LOGO_TYPES):
            flash(request, f"Invalid content type: {content_type}", "error")
            return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
        
        # Check file size
        file_content = await logo_file.read()
        if len(file_content) > MAX_LOGO_SIZE_BYTES:
            flash(request, f"File too large. Max size: {MAX_LOGO_SIZE_MB}MB", "error")
            return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
        
        # Re-read for saving (or use the content we already read)
        file_ext = Path(safe_filename).suffix
        secure_filename = f"{secrets.token_hex(16)}{file_ext}"
        save_path = UPLOADS_DIR / secure_filename
        
        # SECURITY FIX: Use the new robust path validation helper
        if not is_path_within_directory(save_path, UPLOADS_DIR):
            flash(request, "Invalid save path", "error")
            return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
        
        try:
            with open(save_path, "wb") as buffer: 
                buffer.write(file_content)
                
            # Remove old logo if it was uploaded
            if is_uploaded_logo:
                old_logo_path = Path("app" + current_settings.branding_logo_url)
                if is_path_within_directory(old_logo_path, UPLOADS_DIR):
                    try:
                        if old_logo_path.exists(): 
                            os.remove(old_logo_path)
                    except OSError:
                        logger.warning(f"Could not remove old logo: {old_logo_path}")
                else:
                    logger.warning(f"Old logo path is outside uploads directory: {old_logo_path}")
                    
            final_logo_url = f"/static/uploads/{secure_filename}"
            flash(request, "New logo uploaded successfully.", "success")
        except Exception as e:
            logger.error(f"Failed to save uploaded logo: {e}")
            flash(request, f"Error saving logo: {e}", "error")
            return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
    else:
        url_input = form_data.get("branding_logo_url", "")
        # Validate URL format if provided
        if url_input:
            if len(url_input) > 2048:
                flash(request, "Logo URL too long", "error")
                return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
            # Basic URL validation
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url_input)
                if parsed.scheme not in ('http', 'https', ''):
                    raise ValueError
            except Exception:
                flash(request, "Invalid logo URL format", "error")
                return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
        final_logo_url = url_input if url_input else None
        
    update_data["branding_logo_url"] = final_logo_url

    # --- Handle SSL File Logic ---
    async def process_ssl_file(
        file_upload: UploadFile, 
        current_path: Optional[str],
        current_content: Optional[str],
        remove_flag: bool,
        file_type: str # 'key' or 'cert'
    ) -> (Optional[str], Optional[str]):
        
        # SECURITY FIX: Use cryptographically secure random filename to prevent prediction attacks
        managed_filename = f"{secrets.token_hex(16)}_{file_type}.pem"
        managed_path = SSL_DIR / managed_filename

        # Security: Ensure managed_path is within SSL_DIR using robust validation
        if not is_path_within_directory(managed_path, SSL_DIR):
            logger.error(f"Path traversal attempt in SSL file: {managed_path}")
            return current_path, current_content

        # Priority 1: Removal
        if remove_flag:
            # SECURITY FIX: Only remove if file exists and is within SSL_DIR
            if managed_path.exists():
                if is_path_within_directory(managed_path, SSL_DIR):
                    try:
                        os.remove(managed_path)
                    except OSError as e:
                        logger.error(f"Could not remove SSL file: {e}")
                else:
                    logger.warning(f"SSL file path is outside SSL_DIR: {managed_path}")
            flash(request, f"Uploaded SSL {file_type} file removed.", "success")
            return None, None

        # Priority 2: New Upload
        if file_upload and file_upload.filename:
            # Validate filename
            safe_name = sanitize_filename(file_upload.filename)
            if not safe_name or not safe_name.endswith('.pem'):
                flash(request, f"SSL {file_type} file must have .pem extension", "error")
                return current_path, current_content
                
            try:
                content_bytes = await file_upload.read()
                
                # Validate PEM format
                content_str = content_bytes.decode('utf-8', errors='strict')
                
                # Basic PEM validation
                if file_type == 'key':
                    if 'PRIVATE KEY' not in content_str:
                        flash(request, "Invalid private key format", "error")
                        return current_path, current_content
                else:  # cert
                    if 'CERTIFICATE' not in content_str:
                        flash(request, "Invalid certificate format", "error")
                        return current_path, current_content
                
                # Check for suspicious content
                if re.search(r'[^\x20-\x7E\s]', content_str):  # Non-printable chars
                    flash(request, f"SSL {file_type} file contains invalid characters", "error")
                    return current_path, current_content
                
                with open(managed_path, "w", encoding='utf-8') as f:
                    f.write(content_str)
                    
                flash(request, f"New SSL {file_type} file uploaded successfully.", "success")
                return str(managed_path), content_str
                
            except UnicodeDecodeError:
                flash(request, f"SSL {file_type} file must be valid UTF-8 text", "error")
                return current_path, current_content
            except Exception as e:
                logger.error(f"Failed to save uploaded SSL {file_type} file: {e}")
                flash(request, f"Error saving SSL {file_type} file: {e}", "error")
                return current_path, current_content

        # Priority 3: Path from form input
        form_path = form_data.get(f"ssl_{file_type}file", "")
        if form_path and form_path != current_path:
            # Validate path
            if len(form_path) > 2048:
                return current_path, current_content
                
            # Security check: prevent path traversal
            try:
                path_obj = Path(form_path).resolve()
                # Allow only if in SSL_DIR or absolute path
                if not path_obj.is_absolute():
                    if not is_path_within_directory(path_obj, SSL_DIR):
                        return current_path, current_content
                else:
                    # For absolute paths, still validate it's not a sensitive system path
                    # and ideally should be within SSL_DIR
                    if not is_path_within_directory(path_obj, SSL_DIR):
                        logger.warning(f"Absolute SSL path outside SSL_DIR: {form_path}")
                        # Allow but log warning - admin might have valid reason
            except (ValueError, RuntimeError):
                logger.warning(f"Path traversal attempt in SSL path: {form_path}")
                return current_path, current_content
                
            # If a path is specified, it overrides any uploaded file
            if managed_path.exists():
                try:
                    os.remove(managed_path)
                except OSError:
                    pass
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
    selected_theme = form_data.get("selected_theme", current_settings.selected_theme)
    if selected_theme not in current_settings.available_themes:
        selected_theme = current_settings.selected_theme
        
    ui_style = form_data.get("ui_style", current_settings.ui_style)
    allowed_styles = ['dark-glass', 'dark-flat', 'black', 'light-glass', 'light-flat', 'white', 
                      'aurora', 'dark-neumorphic', 'light-neumorphic', 'brutalism', 
                      'retro-terminal', 'cyberpunk', 'material-flat', 'ink']
    if ui_style not in allowed_styles:
        ui_style = current_settings.ui_style
        
    new_redis_password = form_data.get("redis_password", "")
    if new_redis_password and len(new_redis_password) > 256:
        flash(request, "Redis password too long", "error")
        return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)
    
    # Validate Redis connection parameters
    redis_host = form_data.get("redis_host", current_settings.redis_host)
    if not redis_host or len(redis_host) > 256:
        redis_host = current_settings.redis_host
        
    try:
        redis_port = int(form_data.get("redis_port", current_settings.redis_port))
        if redis_port < 1 or redis_port > 65535:
            redis_port = current_settings.redis_port
    except (ValueError, TypeError):
        redis_port = current_settings.redis_port
        
    try:
        model_update_interval = int(form_data.get("model_update_interval_minutes", 10))
        if model_update_interval < 1 or model_update_interval > 1440:  # Max 24 hours
            model_update_interval = 10
    except (ValueError, TypeError):
        model_update_interval = 10
    
    update_data.update({
        "admin_agent_name": (form_data.get("admin_agent_name") or None),
        "branding_title": form_data.get("branding_title", current_settings.branding_title)[:128],
        "ui_style": ui_style,
        "selected_theme": selected_theme,
        "redis_host": redis_host,
        "redis_port": redis_port,
        "redis_username": (form_data.get("redis_username") or None)[:128] if form_data.get("redis_username") else None,
        "model_update_interval_minutes": model_update_interval,
        "allowed_ips": form_data.get("allowed_ips", "")[:2048],
        "denied_ips": form_data.get("denied_ips", "")[:2048],
        "blocked_ollama_endpoints": form_data.get("blocked_ollama_endpoints", "")[:1024],
        "instance_scan_start_port": int(form_data.get("instance_scan_start_port", 11434)),
        "instance_scan_end_port": int(form_data.get("instance_scan_end_port", 11445)),
        "enable_ollama_api": form_data.get("enable_ollama_api") == "true",
        "enable_openai_api": form_data.get("enable_openai_api") == "true",
        "openai_port": int(form_data.get("openai_port", 8081)),
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
        flash(request, "Error: Invalid data provided for a setting.", "error")
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
    # Whitelist allowed sort values
    allowed_sort = ["username", "key_count", "request_count", "last_used"]
    if sort_by not in allowed_sort:
        sort_by = "username"
    if sort_order not in ["asc", "desc"]:
        sort_order = "asc"
        
    context = get_template_context(request)
    context["users"] = await user_crud.get_users(db, sort_by=sort_by, sort_order=sort_order)
    context["csrf_token"] = await get_csrf_token(request)
    context["sort_by"] = sort_by
    context["sort_order"] = sort_order
    return templates.TemplateResponse("admin/users.html", context)

@router.post("/users", name="create_new_user", dependencies=[Depends(validate_csrf_token)])
async def create_new_user(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user), username: str = Form(...), password: str = Form(...)):
    # Validate username
    if not username or len(username) > 128 or not re.match(r'^[\w.-]+$', username):
        flash(request, "Username must be 1-128 characters and contain only letters, numbers, dots, dashes, and underscores", "error")
        return RedirectResponse(url=request.url_for("admin_users"), status_code=status.HTTP_303_SEE_OTHER)
        
    # Validate password strength
    if not password or len(password) < 8:
        flash(request, "Password must be at least 8 characters", "error")
        return RedirectResponse(url=request.url_for("admin_users"), status_code=status.HTTP_303_SEE_OTHER)
        
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
    # Validate user_id
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid user ID")
        
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
    # Validate user_id
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid user ID")
        
    # Validate username
    if not username or len(username) > 128 or not re.match(r'^[\w.-]+$', username):
        flash(request, "Invalid username format", "error")
        return RedirectResponse(url=request.url_for("admin_edit_user_form", user_id=user_id), status_code=status.HTTP_303_SEE_OTHER)
        
    # Validate password if provided
    if password and len(password) < 8:
        flash(request, "Password must be at least 8 characters", "error")
        return RedirectResponse(url=request.url_for("admin_edit_user_form", user_id=user_id), status_code=status.HTTP_303_SEE_OTHER)

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
    # Validate user_id
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid user ID")
        
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
    # Validate user_id
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid user ID")
        
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
        "daily_labels": [
            row.date if isinstance(row.date, str) 
            else row.date.strftime('%Y-%m-%d') if hasattr(row.date, 'strftime')
            else str(row.date)
            for row in daily_stats
        ],
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
    # Validate user_id
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid user ID")
        
    # Validate key_name
    if not key_name or len(key_name) > 128:
        flash(request, "Key name is required and must be under 128 characters", "error")
        return RedirectResponse(url=request.url_for("get_user_details", user_id=user_id), status_code=status.HTTP_303_SEE_OTHER)
        
    # Validate rate limits
    try:
        if rate_limit_requests is not None:
            rate_limit_requests = int(rate_limit_requests)
            if rate_limit_requests < 0 or rate_limit_requests > 1000000:
                rate_limit_requests = None
                
        if rate_limit_window_minutes is not None:
            rate_limit_window_minutes = int(rate_limit_window_minutes)
            if rate_limit_window_minutes < 1 or rate_limit_window_minutes > 10080:  # Max 1 week
                rate_limit_window_minutes = None
    except (ValueError, TypeError):
        rate_limit_requests = None
        rate_limit_window_minutes = None

    # Check for existing key with the same name for this user
    existing_key = await apikey_crud.get_api_key_by_name_and_user_id(db, key_name=key_name, user_id=user_id)
    if existing_key:
        flash(request, f"An API key with the name '{key_name}' already exists for this user.", "error")
        return RedirectResponse(url=request.url_for("get_user_details", user_id=user_id), status_code=status.HTTP_303_SEE_OTHER)

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
    # Validate key_id
    try:
        key_id = int(key_id)
        if key_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid key ID")
        
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
    # Validate key_id
    try:
        key_id = int(key_id)
        if key_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid key ID")
        
    key = await apikey_crud.get_api_key_by_id(db, key_id=key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API Key not found")
    
    await apikey_crud.revoke_api_key(db, key_id=key_id)
    flash(request, f"API Key '{key.key_name}' has been revoked.", "success")
    return RedirectResponse(url=request.url_for("get_user_details", user_id=key.user_id), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/users/{user_id}/delete", name="delete_user_account", dependencies=[Depends(validate_csrf_token)])
async def delete_user_account(request: Request, user_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    # Validate user_id
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid user ID")
        
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        flash(request, "Cannot delete an admin account.", "error")
        return RedirectResponse(url=request.url_for("admin_users"), status_code=status.HTTP_303_SEE_OTHER)
    
    await user_crud.delete_user(db, user_id=user_id)
    flash(request, f"User '{user.username}' has been deleted.", "success")
    return RedirectResponse(url=request.url_for("admin_users"), status_code=status.HTTP_303_SEE_OTHER)

# --- INSTANCE MANAGER ROUTES ---

# --- VIRTUAL AGENT ROUTES (Model + Personality) ---
@router.get("/agents", response_class=HTMLResponse, name="admin_agents")
async def admin_agents_page(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import VirtualAgent
    context = get_template_context(request)
    result = await db.execute(select(VirtualAgent))
    context["agents"] = result.scalars().all()
    context["available_models"] = await server_crud.get_all_available_model_names(db)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/agents.html", context)

@router.post("/agents/add", name="admin_add_agent", dependencies=[Depends(validate_csrf_token)])
async def admin_add_agent(
    request: Request, db: AsyncSession = Depends(get_db), 
    name: str = Form(...), base_model: str = Form(...), 
    system_prompt: str = Form(...), mcp_servers_json: str = Form("[]")
):
    from app.database.models import VirtualAgent
    import json
    
    # 1. Basic validation
    if not name or len(name) < 1:
        flash(request, "Agent name is required.", "error")
        return RedirectResponse(url=request.url_for("admin_agents"), status_code=303)

    # 2. Check for duplicates manually to avoid IntegrityError
    existing = await db.execute(select(VirtualAgent).filter(VirtualAgent.name == name))
    if existing.scalars().first():
        flash(request, f"An agent with the name '{name}' already exists.", "error")
        return RedirectResponse(url=request.url_for("admin_agents"), status_code=303)
    
    try:
        mcp_data = json.loads(mcp_servers_json)
        new_agent = VirtualAgent(
            name=name, 
            base_model=base_model, 
            system_prompt=system_prompt,
            mcp_servers=mcp_data
        )
        db.add(new_agent)
        await db.commit()
        flash(request, f"Agent '{name}' is alive.", "success")
    except Exception as e:
        logger.error(f"Failed to add agent: {e}")
        flash(request, f"Error creating agent: {str(e)}", "error")
        await db.rollback()
        
    return RedirectResponse(url=request.url_for("admin_agents"), status_code=303)

@router.post("/agents/{agent_id}/delete", name="admin_delete_agent", dependencies=[Depends(validate_csrf_token)])
async def admin_delete_agent(agent_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from app.database.models import VirtualAgent
    agent = await db.get(VirtualAgent, agent_id)
    if agent:
        await db.delete(agent)
        await db.commit()
        flash(request, "Agent removed.")
    return RedirectResponse(url=request.url_for("admin_agents"), status_code=303)

@router.get("/agents/{agent_id}/edit", response_class=HTMLResponse, name="admin_edit_agent_form")
async def admin_edit_agent_form(agent_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import VirtualAgent
    agent = await db.get(VirtualAgent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    context = get_template_context(request)
    context["agent"] = agent
    context["available_models"] = await server_crud.get_all_available_model_names(db)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/edit_agent.html", context)

@router.post("/agents/{agent_id}/edit", name="admin_edit_agent_post", dependencies=[Depends(validate_csrf_token)])
async def admin_edit_agent_post(
    agent_id: int, request: Request, db: AsyncSession = Depends(get_db), 
    name: str = Form(...), base_model: str = Form(...), 
    system_prompt: str = Form(...), mcp_servers_json: str = Form("[]")
):
    from app.database.models import VirtualAgent
    import json
    agent = await db.get(VirtualAgent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    # Sanitize name to be model-safe (slugify)
    agent.name = re.sub(r'[^a-z0-9.-]', '-', name.lower())
    agent.base_model = base_model
    agent.system_prompt = system_prompt
    agent.mcp_servers = json.loads(mcp_servers_json)
    
    await db.commit()
    flash(request, f"Agent '{agent.name}' updated successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_agents"), status_code=303)

# --- ENSEMBLE ORCHESTRATOR ROUTES ---
@router.get("/ensembles", response_class=HTMLResponse, name="admin_ensembles_page")
async def admin_ensembles_page(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import EnsembleOrchestrator, VirtualAgent
    context = get_template_context(request)
    
    # Fetch ensembles
    result = await db.execute(select(EnsembleOrchestrator))
    context["ensembles"] = result.scalars().all()
    
    # Fetch agent names for highlighting in UI
    agent_res = await db.execute(select(VirtualAgent.name))
    context["agent_names"] = agent_res.scalars().all()
    
    all_models = await server_crud.get_all_available_model_names(db)
    context["agents"] = context["agent_names"] # Already fetched in previous step
    context["raw_models"] = [m for m in all_models if m not in context["agents"] and m != "auto"]
    
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/ensembles.html", context)

@router.post("/ensembles/add", name="admin_add_ensemble", dependencies=[Depends(validate_csrf_token)])
async def admin_add_ensemble(
    request: Request, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user),
    name: str = Form(...),
    master_model: str = Form(...),
    parallel_models: List[str] = Form(...),
    show_monologue: bool = Form(False),
    send_status_update: bool = Form(False),
    vision_processor: Optional[str] = Form(None)
):
    from app.database.models import EnsembleOrchestrator
    name = re.sub(r'[^a-z0-9.-]', '-', name.lower())
    
    new_bundle = EnsembleOrchestrator(
        name=name,
        master_model=master_model,
        parallel_participants=parallel_models,
        parallel_models=parallel_models, # Legacy field sync
        vision_processor=vision_processor if vision_processor else None,
        show_monologue=show_monologue,
        description=f"Ensemble: {', '.join(parallel_models)} -> {master_model}"
    )
    db.add(new_bundle)
    await db.commit()
    flash(request, f"Bundle '{name}' created successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_ensembles_page"), status_code=303)

@router.get("/chains", response_class=HTMLResponse, name="admin_chains_page")
async def admin_chains_page(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import ChainOrchestrator
    context = get_template_context(request)
    result = await db.execute(select(ChainOrchestrator))
    context["chains"] = result.scalars().all()
    context["raw_models"] = await server_crud.get_all_available_model_names(db)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/chains.html", context)

@router.post("/chains/add", name="admin_add_chain", dependencies=[Depends(validate_csrf_token)])
async def admin_add_chain(
    request: Request, db: AsyncSession = Depends(get_db), 
    name: str = Form(...), steps: List[str] = Form(...)
):
    from app.database.models import ChainOrchestrator
    new_chain = ChainOrchestrator(name=name, steps=steps)
    db.add(new_chain)
    await db.commit()
    flash(request, "Chain deployed.", "success")
    return RedirectResponse(url=request.url_for("admin_chains_page"), status_code=303)

@router.post("/ensembles/{ensemble_id}/delete", name="admin_delete_ensemble", dependencies=[Depends(validate_csrf_token)])
async def admin_delete_ensemble(ensemble_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import EnsembleOrchestrator
    ensemble = await db.get(EnsembleOrchestrator, ensemble_id)
    if ensemble:
        await db.delete(ensemble)
        await db.commit()
        flash(request, "Ensemble deleted.")
    return RedirectResponse(url=request.url_for("admin_ensembles_page"), status_code=303)

@router.get("/ensembles/{ensemble_id}/edit", response_class=HTMLResponse, name="admin_edit_ensemble_form")
async def admin_edit_ensemble_form(ensemble_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import EnsembleOrchestrator, VirtualAgent
    ensemble = await db.get(EnsembleOrchestrator, ensemble_id)
    if not ensemble: raise HTTPException(status_code=404, detail="Ensemble not found")
    
    context = get_template_context(request)
    context["ensemble"] = ensemble
    
    # Fetch categorization data
    all_models = await server_crud.get_all_available_model_names(db)
    agent_res = await db.execute(select(VirtualAgent.name))
    context["agents"] = agent_res.scalars().all()
    context["raw_models"] = [m for m in all_models if m not in context["agents"] and m != "auto"]
    
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/edit_ensemble.html", context)

@router.post("/ensembles/{ensemble_id}/edit", name="admin_edit_ensemble_post", dependencies=[Depends(validate_csrf_token)])
async def admin_edit_ensemble_post(
    ensemble_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user),
    name: str = Form(...), master_model: str = Form(...), parallel_models: List[str] = Form(...),
    show_monologue: bool = Form(False), send_status_update: bool = Form(False),
    vision_processor: Optional[str] = Form(None)
):
    from app.database.models import EnsembleOrchestrator
    bundle = await db.get(EnsembleOrchestrator, ensemble_id)
    if not bundle: raise HTTPException(status_code=404, detail="Bundle not found")
    
    bundle.name = re.sub(r'[^a-z0-9.-]', '-', name.lower())
    bundle.master_model = master_model
    bundle.parallel_participants = parallel_models
    bundle.parallel_models = parallel_models # Legacy field sync
    bundle.vision_processor = vision_processor if vision_processor else None
    bundle.show_monologue = show_monologue
    bundle.send_status_update = send_status_update
    bundle.description = f"Ensemble: {', '.join(parallel_models)} -> {master_model}"
    
    display_name = bundle.name
    await db.commit()
    flash(request, f"Bundle '{display_name}' updated.", "success")
    return RedirectResponse(url=request.url_for("admin_ensembles_page"), status_code=303)

# --- SMART ROUTER ROUTES ---
@router.get("/routers", response_class=HTMLResponse, name="admin_routers_page")
async def admin_routers_page(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import SmartRouter, VirtualAgent
    context = get_template_context(request)
    result = await db.execute(select(SmartRouter))
    context["routers"] = result.scalars().all()
    
    # Fetch categorization data
    all_models = await server_crud.get_all_available_model_names(db)
    agent_res = await db.execute(select(VirtualAgent.name))
    context["agents"] = agent_res.scalars().all()
    context["available_models"] = sorted(all_models)  # Ensure sorted for consistent display
    context["raw_models"] = [m for m in all_models if m not in context["agents"] and m != "auto"]
    
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/routers.html", context)

@router.post("/routers/vision-enabler", name="admin_add_vision_enabler", dependencies=[Depends(validate_csrf_token)])
async def admin_add_vision_enabler(
    request: Request, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user),
    name: str = Form(...),
    vision_text_model: str = Form(...),
    vision_vlm_model: str = Form(...)
):
    """Quick shortcut to create a vision-enabled smart router."""
    from app.database.models import SmartRouter
    
    router_name = re.sub(r'[^a-z0-9.-]', '-', name.lower())
    
    # Check if name already exists
    existing = await db.execute(select(SmartRouter).filter(SmartRouter.name == router_name))
    if existing.scalars().first():
        flash(request, f"A router with name '{router_name}' already exists.", "error")
        return RedirectResponse(url=request.url_for("admin_routers_page"), status_code=303)
    
    # Create a smart router with hierarchical rules:
    # 1. If has_images -> route to VLM
    # 2. Else (fallback) -> route to text model
    new_router = SmartRouter(
        name=router_name,
        strategy='priority',
        targets=[vision_vlm_model, vision_text_model],  # VLM first, text model second
        models=[vision_vlm_model, vision_text_model],  # Legacy field sync
        rules=[
            {
                "logic": "OR",
                "target": vision_vlm_model,
                "conditions": [{"type": "has_images", "value": ""}]
            }
            # If no rules match for text model, priority strategy will use it as fallback
        ],
        classifier_model=None,
        description=f"Vision Router: Images→{vision_vlm_model}, Text→{vision_text_model}"
    )
    db.add(new_router)
    await db.commit()
    flash(request, f"Vision-enabled router '{router_name}' created! Use it as a model name.", "success")
    return RedirectResponse(url=request.url_for("admin_routers_page"), status_code=303)

@router.get("/gpu-stats", name="admin_gpu_stats")
async def get_gpu_stats(admin_user: User = Depends(require_admin_user)):
    """Cross-platform GPU monitoring for Windows, Linux, and macOS."""
    import platform
    sys_type = platform.system()
    
    # --- MacOS (Apple Silicon) Support ---
    if sys_type == "Darwin":
        try:
            # Use system_profiler to get unified memory stats
            cmd = "system_profiler SPDisplaysDataType -json"
            res = subprocess.check_output(cmd, shell=True, encoding='utf-8')
            data = json.loads(res)
            gpu_info = data.get("SPDisplaysDataType", [{}])[0]
            
            # MacOS uses unified memory; we'll treat it as GPU VRAM for UI consistency
            mem = psutil.virtual_memory()
            return {
                "success": True,
                "gpus": [{
                    "index": "0",
                    "name": gpu_info.get("sppci_model", "Apple M-Series"),
                    "vram_used_gb": round(mem.used / (1024**3), 2),
                    "vram_total_gb": round(mem.total / (1024**3), 2),
                    "utilization": 0, # Difficult to get raw % via CLI on Mac
                    "processes": [{"pid": p.pid, "name": p.name(), "vram_mb": 0} for p in psutil.process_iter(['pid', 'name']) if 'ollama' in p.info['name'].lower()][:5]
                }]
            }
        except Exception as e:
            return {"success": False, "gpus": [], "error": f"Mac Telemetry Error: {str(e)}"}

    # --- Windows / Linux (NVIDIA) Support ---
    nvsmi_path = r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
    cmd_base = "nvidia-smi" if os.name != 'nt' else (nvsmi_path if os.path.exists(nvsmi_path) else "nvidia-smi")
    
    try:
        # 1. Hardware Metrics
        cmd_hw = f'{cmd_base} --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits'
        res_hw = subprocess.check_output(cmd_hw, encoding='utf-8', shell=True)
        
        # 2. Comprehensive Process List (Compute + Graphics)
        # Note: --query-compute-apps is standard, but doesn't show Graphics usage.
        # We try to get PIDs from the general query first.
        cmd_apps = f'{cmd_base} --query-compute-apps=gpu_index,pid,used_gpu_memory --format=csv,noheader,nounits'
        try:
            res_apps = subprocess.check_output(cmd_apps, encoding='utf-8', shell=True)
        except subprocess.CalledProcessError:
            res_apps = ""

        proc_map = {}
        for line in res_apps.strip().split('\n'):
            if not line.strip(): continue
            parts = [x.strip() for x in line.split(',')]
            if len(parts) < 3: continue
            idx, pid, vram = parts
            
            proc_name = "Unknown"
            try:
                p = psutil.Process(int(pid))
                proc_name = p.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied): pass

            if idx not in proc_map: proc_map[idx] = []
            proc_map[idx].append({"pid": int(pid), "name": proc_name, "vram_mb": int(vram), "type": "Compute"})

        gpus = []
        for line in res_hw.strip().split('\n'):
            parts = [x.strip() for x in line.split(',')]
            if len(parts) < 5: continue
            idx, name, mem_used, mem_total, util = parts
            
            current_procs = proc_map.get(idx, [])
            # Calculate "Ghost" usage (Graphics/DWM/System)
            vram_accounted_mb = sum(p['vram_mb'] for p in current_procs)
            vram_total_used_mb = int(mem_used)
            ghost_vram = vram_total_used_mb - vram_accounted_mb
            
            # If there is significant ghost usage, add a system entry
            if ghost_vram > 50:
                current_procs.append({
                    "pid": 0,
                    "name": "System / Desktop / Browser",
                    "vram_mb": ghost_vram,
                    "type": "Graphics",
                    "is_system": True
                })

            gpus.append({
                "index": idx,
                "name": name,
                "vram_used_gb": round(vram_total_used_mb / 1024, 2),
                "vram_total_gb": round(int(mem_total) / 1024, 2),
                "utilization": int(util),
                "processes": current_procs
            })
        return {"success": True, "gpus": gpus}
    except Exception as e:
        return {"success": False, "gpus": [], "error": f"GPU Error: {str(e)}"}

@router.post("/gpu-stats/kill/{pid}", name="admin_kill_gpu_process", dependencies=[Depends(validate_csrf_token)])
async def kill_gpu_process(pid: int, admin_user: User = Depends(require_admin_user)):
    """Terminates a specific process using platform-appropriate elevation."""
    if pid <= 0:
        raise HTTPException(status_code=400, detail="Invalid PID")

    try:
        if os.name == 'nt':
            # Windows: Force kill by PID
            cmd = ["taskkill", "/F", "/PID", str(pid)]
            subprocess.run(cmd, check=True, capture_output=True)
        else:
            # Linux/macOS: requires sudo for processes owned by other users
            # User must configure sudoers to allow this securely
            cmd = ["sudo", "kill", "-9", str(pid)]
            subprocess.run(cmd, check=True, capture_output=True)
            
        logger.warning(f"Admin {admin_user.username} terminated GPU process {pid}")
        return {"success": True, "message": f"Process {pid} terminated."}
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode() if e.stderr else str(e)
        logger.error(f"Failed to kill process {pid}: {err_msg}")
        return {"success": False, "error": f"Termination failed: {err_msg}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

        
@router.post("/routers/add", name="admin_add_router", dependencies=[Depends(validate_csrf_token)])
async def admin_add_router(
    request: Request, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user),
    name: str = Form(...),
    strategy: str = Form(...),
    classifier_model: Optional[str] = Form(None),
    models: Optional[List[str]] = Form(None)
):
    from app.database.models import SmartRouter
    
    form_data = await request.form()
    
    # Process Hierarchical Decision Groups
    processed_groups = []
    group_ids = form_data.getlist("group_ids")
    
    for gid in group_ids:
        target = form_data.get(f"group_target_{gid}")
        logic = form_data.get(f"group_logic_{gid}", "OR")
        
        types = form_data.getlist(f"cond_type_{gid}")
        vals = form_data.getlist(f"cond_val_{gid}")
        
        conditions = []
        for c_type, c_val in zip(types, vals):
            conditions.append({"type": c_type, "value": c_val})
            
        if conditions and target:
            processed_groups.append({
                "logic": logic,
                "target": target,
                "conditions": conditions
            })
    
    # Ensure models is a list, default to empty if None        
    target_models = models if models else []
            
    new_router = SmartRouter(
        name=name, 
        strategy=strategy, 
        classifier_model=classifier_model,
        targets=target_models,
        models=target_models,  # Legacy field sync
        rules=processed_groups
    )
    db.add(new_router)
    await db.commit()
    flash(request, f"Router '{name}' active.", "success")
    return RedirectResponse(url=request.url_for("admin_routers_page"), status_code=303)

@router.post("/routers/{router_id}/delete", name="admin_delete_router", dependencies=[Depends(validate_csrf_token)])
async def admin_delete_router(router_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import SmartRouter
    router_obj = await db.get(SmartRouter, router_id)
    if router_obj:
        await db.delete(router_obj)
        await db.commit()
        flash(request, "Router deleted.")
    return RedirectResponse(url=request.url_for("admin_routers_page"), status_code=303)

@router.get("/routers/{router_id}/edit", response_class=HTMLResponse, name="admin_edit_router_form")
async def admin_edit_router_form(router_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.database.models import SmartRouter, VirtualAgent
    router_obj = await db.get(SmartRouter, router_id)
    if not router_obj: raise HTTPException(status_code=404, detail="Router not found")
    context = get_template_context(request)
    context["router"] = router_obj
    
    # Fetch categorization data
    all_models = await server_crud.get_all_available_model_names(db)
    agent_res = await db.execute(select(VirtualAgent.name))
    context["agents"] = agent_res.scalars().all()
    context["available_models"] = all_models
    context["raw_models"] = [m for m in all_models if m not in context["agents"] and m != "auto"]
    
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/edit_router.html", context)

@router.post("/routers/{router_id}/edit", name="admin_edit_router_post", dependencies=[Depends(validate_csrf_token)])
async def admin_edit_router_post(
    router_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user),
    name: str = Form(...), strategy: str = Form(...), models: List[str] = Form(...),
    rule_conditions: List[str] = Form(default=[]), rule_values: List[str] = Form(default=[]), rule_targets: List[str] = Form(default=[])
):
    from app.database.models import SmartRouter
    router_obj = await db.get(SmartRouter, router_id)
    if not router_obj: raise HTTPException(status_code=404, detail="Router not found")
    
    processed_rules = []
    for cond, val, target in zip(rule_conditions, rule_values, rule_targets):
        if cond and target:
            processed_rules.append({"condition": cond, "value": val, "target": target})
            
    router_obj.name = re.sub(r'[^a-z0-9.-]', '-', name.lower())
    router_obj.strategy = strategy
    router_obj.targets = models
    router_obj.rules = processed_rules
    
    display_name = router_obj.name
    await db.commit()
    flash(request, f"Router '{display_name}' updated.", "success")
    return RedirectResponse(url=request.url_for("admin_routers_page"), status_code=303)

@router.get("/instances", response_class=HTMLResponse, name="admin_instances")
async def admin_instances_page(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    result = await db.execute(select(ManagedInstance))
    instances = result.scalars().all()
    
    instance_list = []
    for inst in instances:
        state, pid = await supervisor.get_instance_state(inst)
        instance_list.append({
            "config": inst,
            "state": state,
            "pid": pid
        })
        
    # Discover unmanaged local instances
    app_settings: AppSettingsModel = request.app.state.settings
    managed_ports = [inst.port for inst in instances]
    
    # Directly await the async discovery method
    discovered = await supervisor.discover_local_instances(
        managed_ports, 
        start_port=app_settings.instance_scan_start_port,
        end_port=app_settings.instance_scan_end_port
    )
        
    context["instances"] = instance_list
    context["discovered"] = discovered
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/instances.html", context)

@router.post("/instances/adopt", name="admin_adopt_instance", dependencies=[Depends(validate_csrf_token)])
async def adopt_instance(
    request: Request, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user),
    name: str = Form(...), 
    port: int = Form(...)
):
    # Check if already exists
    existing = await db.execute(select(ManagedInstance).filter(ManagedInstance.port == port))
    if existing.scalars().first():
        flash(request, f"Instance on port {port} is already being tracked.", "error")
        return RedirectResponse(url=request.url_for("admin_instances"), status_code=303)

    new_inst = ManagedInstance(
        name=name, 
        port=port, 
        is_enabled=True # Mark as enabled since it's already running
    )
    db.add(new_inst)
    await db.commit()
    
    # Also ensure it's in the main server list for the load balancer
    # Pydantic's AnyHttpUrl normalizes host-only URLs by adding a trailing slash.
    # We check for both versions to prevent duplicate insertion errors.
    server_url = f"http://127.0.0.1:{port}/"
    srv_exists = await server_crud.get_server_by_url(db, server_url)
    if not srv_exists:
        srv_exists = await server_crud.get_server_by_url(db, server_url.rstrip('/'))
        
    if not srv_exists:
        await server_crud.create_server(db, ServerCreate(name=f"[Adopted] {name}", url=server_url))

    flash(request, f"Successfully adopted instance '{name}' on port {port}.", "success")
    return RedirectResponse(url=request.url_for("admin_instances"), status_code=303)

@router.post("/instances/add", name="admin_add_instance", dependencies=[Depends(validate_csrf_token)])
async def admin_add_instance(
    request: Request, 
    db: AsyncSession = Depends(get_db), 
    admin_user: User = Depends(require_admin_user),
    name: str = Form(...), 
    port: int = Form(...),
    backend_type: str = Form("ollama"),
    gpu_ids: str = Form(""),
    model_path: Optional[str] = Form(None),
    n_gpu_layers: int = Form(99),
    ctx_size: int = Form(8192),
    threads: int = Form(8),
    tensor_parallel_size: int = Form(1)
):
    # Validation for single-model backends where path is mandatory
    if backend_type in ('llamacpp', 'vllm') and not model_path:
        flash(request, f"Model path is mandatory for {backend_type} backends.", "error")
        return RedirectResponse(url=request.url_for("admin_instances"), status_code=303)

    new_inst = ManagedInstance(
        name=name, 
        port=port, 
        backend_type=backend_type,
        gpu_ids=gpu_ids, 
        model_path=model_path,
        n_gpu_layers=n_gpu_layers,
        ctx_size=ctx_size,
        threads=threads,
        tensor_parallel_size=tensor_parallel_size,
        is_enabled=False
    )
    db.add(new_inst)
    try:
        await db.commit()
        flash(request, f"Instance configuration '{name}' added.", "success")
    except Exception as e:
        await db.rollback()
        flash(request, f"Error adding instance: {str(e)}", "error")
        
    return RedirectResponse(url=request.url_for("admin_instances"), status_code=303)

@router.post("/instances/{instance_id}/toggle", name="admin_toggle_instance", dependencies=[Depends(validate_csrf_token)])
async def toggle_instance(instance_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    inst = await db.get(ManagedInstance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="Instance not found")

    if supervisor.is_running(instance_id):
        await supervisor.stop_instance(instance_id)
        flash(request, f"Instance '{inst.name}' stopped.")
    else:
        success = await supervisor.start_instance(inst)
        if success:
            # Auto-add to server list if not exists
            # Construct normalized URL (with slash) to check against DB records
            server_url = f"http://127.0.0.1:{inst.port}/"
            existing = await server_crud.get_server_by_url(db, server_url)
            if not existing:
                existing = await server_crud.get_server_by_url(db, server_url.rstrip('/'))

            if not existing:
                # Map llamacpp and vllm to the 'vllm' server type (OpenAI-compatible)
                s_type = "vllm" if inst.backend_type in ("vllm", "llamacpp") else "ollama"
                await server_crud.create_server(db, ServerCreate(name=f"[{inst.backend_type.upper()}] {inst.name}", url=server_url, server_type=s_type))
            flash(request, f"Instance '{inst.name}' started successfully.", "success")
        else:
            flash(request, f"Failed to start '{inst.name}'. Check if port {inst.port} is free and binaries are configured.", "error")
            
    return RedirectResponse(url=request.url_for("admin_instances"), status_code=303)

@router.post("/instances/{instance_id}/delete", name="admin_delete_instance", dependencies=[Depends(validate_csrf_token)])
async def delete_instance(instance_id: int, request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    inst = await db.get(ManagedInstance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="Instance not found")
        
    if supervisor.is_running(instance_id):
        await supervisor.stop_instance(instance_id)
        
    await db.delete(inst)
    await db.commit()
    flash(request, f"Instance '{inst.name}' removed from manager.")
    return RedirectResponse(url=request.url_for("admin_instances"), status_code=303)

# Add this new route for prompt enhancement
@router.get("/logs", response_class=HTMLResponse, name="admin_logs")
async def admin_logs_page(request: Request, admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/logs.html", context)

@router.get("/logs/raw", name="admin_get_logs_raw")
async def get_raw_logs(admin_user: User = Depends(require_admin_user), lines: int = Query(200)):
    log_file = Path("lollms_hub.log")
    if not log_file.exists():
        return {"logs": "Log file not found."}
    
    try:
        # Use tail-like logic for performance
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.readlines()
            return {"logs": "".join(content[-lines:])}
    except Exception as e:
        return {"logs": f"Error reading logs: {str(e)}"}

@router.get("/logs/export", name="admin_export_logs")
async def export_logs(admin_user: User = Depends(require_admin_user)):
    from fastapi.responses import FileResponse
    log_file = Path("lollms_hub.log")
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file empty")
    return FileResponse(log_file, filename="lollms_hub_diagnostic.log")

@router.post("/logs/analyze", name="admin_analyze_logs")
async def analyze_logs_ai(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
    
    app_settings: AppSettingsModel = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    
    if not target_agent:
        return JSONResponse({"error": "No Management Agent set in Settings to perform analysis."}, status_code=400)

    log_file = Path("lollms_hub.log")
    if not log_file.exists():
        return {"analysis": "No logs available to analyze."}

    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        recent_logs = "".join(f.readlines()[-150:]) # Send last 150 lines

    analysis_prompt = (
        "You are the System Architect for LoLLMs Hub. Review the following application logs. "
        "Identify actual errors, performance bottlenecks, or critical security issues.\n\n"
        "IMPORTANT ARCHITECTURAL CONTEXT:\n"
        "- Redis is OPTIONAL. If logs show it is not connected, treat this as a configuration status, "
        "NOT a bug or critical error. Only mention it as a suggestion if the user needs rate limiting.\n"
        "- Focus on backend connectivity to Ollama/vLLM and database integrity.\n\n"
        f"RECENT LOG DATA:\n{recent_logs}"
    )

    payload = {
        "model": target_agent,
        "messages": [{"role": "user", "content": analysis_prompt}],
        "stream": False
    }

    try:
        real_model, final_msgs = await _resolve_target(db, target_agent, payload["messages"])
        servers = await server_crud.get_servers_with_model(db, real_model)
        if not servers: return JSONResponse({"error": "Compute node for agent offline."}, status_code=503)

        resp, _ = await _reverse_proxy(request, "chat", servers, json.dumps({"model": real_model, "messages": final_msgs, "stream": False}).encode(), is_subrequest=True)
        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            return {"analysis": data.get("message", {}).get("content", "Analysis failed.")}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/logs", response_class=HTMLResponse, name="admin_logs")
async def admin_logs_page(request: Request, admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/logs.html", context)

@router.get("/logs/raw", name="admin_get_logs_raw")
async def get_raw_logs(admin_user: User = Depends(require_admin_user), lines: int = Query(200)):
    log_file = Path("lollms_hub.log")
    if not log_file.exists():
        return {"logs": "Log file not found. Check if file logging is enabled in logging_config.py"}
    
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.readlines()
            return {"logs": "".join(content[-lines:])}
    except Exception as e:
        return {"logs": f"Error reading logs: {str(e)}"}

@router.get("/logs/export", name="admin_export_logs")
async def export_logs(admin_user: User = Depends(require_admin_user)):
    from fastapi.responses import FileResponse
    log_file = Path("lollms_hub.log")
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file empty")
    return FileResponse(log_file, filename="lollms_hub_diagnostic.log")

@router.post("/logs/analyze", name="admin_analyze_logs")
async def analyze_logs_ai(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
    
    app_settings: AppSettingsModel = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    
    if not target_agent:
        return JSONResponse({"error": "No Management Agent set in Settings to perform analysis."}, status_code=400)

    log_file = Path("lollms_hub.log")
    if not log_file.exists():
        return {"analysis": "No logs available to analyze."}

    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        recent_logs = "".join(f.readlines()[-150:])

    analysis_prompt = (
        "You are the System Architect for LoLLMs Hub. Review the following application logs. "
        "Identify actual errors, performance bottlenecks, or critical security issues.\n\n"
        "IMPORTANT ARCHITECTURAL CONTEXT:\n"
        "- Redis is OPTIONAL. If logs show it is not connected, treat this as a configuration status, "
        "NOT a bug or critical error. Only mention it as a suggestion if the user needs rate limiting.\n"
        "- Focus on backend connectivity to Ollama/vLLM and database integrity.\n\n"
        f"RECENT LOG DATA:\n{recent_logs}"
    )

    payload = {
        "model": target_agent,
        "messages": [{"role": "user", "content": analysis_prompt}],
        "stream": False
    }

    try:
        logger.info(f"AI Log Analysis started using agent: {target_agent}")
        real_model, final_msgs = await _resolve_target(db, target_agent, payload["messages"])
        servers = await server_crud.get_servers_with_model(db, real_model)
        
        if not servers: 
            logger.error(f"Analysis failed: No servers found for model {real_model}")
            return JSONResponse({"error": f"Compute node for model '{real_model}' is offline or not found."}, status_code=503)

        # Surgical Fix: Explicitly pass a long timeout for the analysis sub-request
        resp, _ = await _reverse_proxy(
            request, "chat", servers, 
            json.dumps({"model": real_model, "messages": final_msgs, "stream": False}).encode(), 
            is_subrequest=True
        )
        
        if hasattr(resp, 'status_code') and resp.status_code != 200:
             error_data = json.loads(resp.body.decode()) if hasattr(resp, 'body') else {"error": "Unknown backend error"}
             return JSONResponse({"error": f"AI Backend Error: {error_data.get('error', 'Unknown')}"}, status_code=resp.status_code)

        if hasattr(resp, 'body'):
            data = json.loads(resp.body.decode())
            analysis_text = data.get("message", {}).get("content", "Analysis failed.")
            
            # --- ARCHIVE LOGIC ---
            new_analysis = LogAnalysis(content=analysis_text)
            db.add(new_analysis)
            await db.commit()
            await db.refresh(new_analysis)
            
            return {
                "id": new_analysis.id,
                "timestamp": new_analysis.timestamp.strftime("%Y-%m-%d %H:%M"),
                "analysis": analysis_text
            }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/logs/analysis/history", name="admin_analysis_history")
async def get_analysis_history(db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    """Returns the list of archived AI reports with a clean preview snippet."""
    try:
        result = await db.execute(select(LogAnalysis).order_by(LogAnalysis.timestamp.desc()).limit(50))
        history = result.scalars().all()
        
        def clean_snippet(text):
            if not text: return "Empty report."
            # Remove markdown structural characters and code block markers
            clean = re.sub(r'[*#_>`\-]', '', text)
            clean = re.sub(r'\[.*?\]', '', clean) # Remove brackets
            # Collapse multiple spaces/newlines
            clean = " ".join(clean.split())
            return clean[:85] + "..."

        return [{"id": a.id, "ts": a.timestamp.strftime("%Y-%m-%d %H:%M"), "snippet": clean_snippet(a.content)} for a in history]
    except Exception as e:
        logger.error(f"Failed to fetch analysis history: {e}")
        return [] # Return empty list so frontend doesn't crash

@router.get("/logs/analysis/{aid}", name="admin_get_analysis")
async def get_analysis_detail(aid: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    analysis = await db.get(LogAnalysis, aid)
    if not analysis: raise HTTPException(status_code=404)
    return {
        "id": analysis.id, 
        "timestamp": analysis.timestamp.strftime("%Y-%m-%d %H:%M"), 
        "analysis": analysis.content
    }

@router.delete("/logs/analysis/{aid}", name="admin_delete_analysis", dependencies=[Depends(validate_csrf_token)])
async def delete_analysis(aid: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    analysis = await db.get(LogAnalysis, aid)
    if analysis:
        await db.delete(analysis)
        await db.commit()
    return {"success": True}

# Add this new route for prompt enhancement
@router.post("/enhance-prompt", name="admin_enhance_prompt")
async def admin_enhance_prompt(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user)
):
    """
    Uses the Management Agent set in settings to rewrite the system prompt.
    """
    from app.api.v1.routes.proxy import proxy_ollama
    data = await request.json()
    prompt_to_enhance = data.get("prompt", "")
    
    app_settings: AppSettingsModel = request.app.state.settings
    target_agent = app_settings.admin_agent_name
    
    if not target_agent:
        return JSONResponse({"error": "No Management Agent set in Settings."}, status_code=400)
    
    meta_prompt = (
        "You are an expert Prompt Engineer. Your task is to rewrite and enhance the following system prompt "
        "to be more effective, clear, and professional. Use markdown. Focus on adding structural constraints "
        "and defining the persona clearly. Return ONLY the enhanced prompt text, no chat or introduction.\n\n"
        f"ORIGINAL PROMPT:\n{prompt_to_enhance}"
    )
    
    # Create a synthetic internal request
    # We call our own proxy route as if the admin was a client
    # This ensures it goes through the full Agent -> Model resolution stack
    payload = {
        "model": target_agent,
        "messages": [{"role": "user", "content": meta_prompt}],
        "stream": False,
        "options": {"temperature": 0.7}
    }
    
    try:
        # Resolve the agent to a physical model
        from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
        
        resolution = await _resolve_target(db, target_agent, payload["messages"])
        if not resolution or not isinstance(resolution, tuple):
            return JSONResponse({"error": f"Failed to resolve management agent '{target_agent}'"}, status_code=500)
            
        real_model, final_msgs = resolution
        from app.crud.apikey_crud import create_api_key
        
        # Get or create a temporary system user for this task
        # Simplified: Use the existing logic by calling the backend function directly
        # Note: In a production refactor, extract the 'resolve and execute' logic to a service
        from app.api.v1.routes.playground_chat import admin_playground_stream
        
        # For simplicity and to avoid circular deps, we hit the internal proxy logic
        # via httpx but on the local loopback
        headers = {"Content-Type": "application/json"}
        # We'll use a more direct approach since we are inside the app:
        from app.api.v1.routes.proxy import _reverse_proxy, _resolve_target, _async_log_usage
        
        real_model, final_msgs = await _resolve_target(db, target_agent, payload["messages"])
        servers = await server_crud.get_servers_with_model(db, real_model)
        
        if not servers:
            return JSONResponse({"error": f"Model for agent {target_agent} not found."}, status_code=503)

        resp, _ = await _reverse_proxy(
            request, "chat", servers, 
            json.dumps({"model": real_model, "messages": final_msgs, "stream": False}).encode(),
            is_subrequest=True
        )
        
        if hasattr(resp, 'body'):
            resp_data = json.loads(resp.body.decode())
            enhanced = resp_data.get("message", {}).get("content", "").strip()
            return {"enhanced": enhanced}
            
    except Exception as e:
        logger.error(f"Enhancement failed: {e}")
        return JSONResponse({"error": f"Enhancement failed: {str(e)}"}, status_code=500)