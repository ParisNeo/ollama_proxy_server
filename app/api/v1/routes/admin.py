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
    
    # Ensure metadata exists for all discovered models
    all_model_names = await server_crud.get_all_available_model_names(db)
    for model_name in all_model_names:
        await model_metadata_crud.get_or_create_metadata(db, model_name=model_name)
        
    context["metadata_list"] = await model_metadata_crud.get_all_metadata(db)
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
                "priority": int(form_data.get(f"priority_{meta_id}", 10)),
            }
            await model_metadata_crud.update_metadata(db, model_name=metadata.model_name, **update_data)

    flash(request, "Model metadata updated successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_models_manager"), status_code=status.HTTP_303_SEE_OTHER)


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
        "redis_port": int(form_data.get("redis_port", 6379)),
        "redis_username": form_data.get("redis_username") or None,
        "model_update_interval_minutes": int(form_data.get("model_update_interval_minutes", 10)),
        "allowed_ips": form_data.get("allowed_ips", ""),
        "denied_ips": form_data.get("denied_ips", ""),
        "blocked_ollama_endpoints": form_data.get("blocked_ollama_endpoints", ""),
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