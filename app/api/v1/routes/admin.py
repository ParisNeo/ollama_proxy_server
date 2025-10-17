# app/api/v1/routes/admin.py
import logging
from typing import Union, Optional, List
import redis.asyncio as redis

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import verify_password
from app.database.session import get_db
from app.database.models import User
from app.crud import user_crud, apikey_crud, log_crud, server_crud, settings_crud
from app.schema.user import UserCreate
from app.schema.server import ServerCreate
from app.schema.settings import AppSettingsModel
from app.api.v1.dependencies import get_csrf_token, validate_csrf_token, login_rate_limiter

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# --- Helper to add common context to all templates ---
def get_template_context(request: Request) -> dict:
    return {
        "request": request,
        "is_redis_connected": request.app.state.redis is not None,
        "bootstrap_settings": settings
    }

def flash(request: Request, message: str, category: str = "info"):
    if "_messages" not in request.session: request.session["_messages"] = []
    request.session["_messages"].append({"message": message, "category": category})
def get_flashed_messages(request: Request): return request.session.pop("_messages", [])
templates.env.globals["get_flashed_messages"] = get_flashed_messages
async def get_current_user_from_cookie(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    if user_id: return await user_crud.get_user_by_id(db, user_id=user_id)
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
    context["users"] = await user_crud.get_users(db)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/dashboard.html", context)
    
@router.get("/stats", response_class=HTMLResponse, name="admin_stats")
async def admin_stats(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    key_usage_stats = await log_crud.get_usage_statistics(db)
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
        "model_data": [row.request_count for row in model_stats]
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
async def admin_add_server(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user), server_name: str = Form(...), server_url: str = Form(...)):
    existing_server = await server_crud.get_server_by_url(db, url=server_url)
    if existing_server: flash(request, f"Server with URL '{server_url}' already exists.", "error")
    else:
        try:
            server_in = ServerCreate(name=server_name, url=server_url)
            await server_crud.create_server(db, server=server_in)
            flash(request, f"Server '{server_name}' added successfully.", "success")
        except Exception: flash(request, "Invalid URL format.", "error")
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

@router.get("/settings", response_class=HTMLResponse, name="admin_settings")
async def admin_settings_form(request: Request, admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    context["settings"] = request.app.state.settings
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/settings.html", context)

@router.post("/settings", name="admin_settings_post", dependencies=[Depends(validate_csrf_token)])
async def admin_settings_post(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    form_data = await request.form()
    
    current_settings: AppSettingsModel = request.app.state.settings
    new_redis_password = form_data.get("redis_password")

    try:
        updated_settings_data = AppSettingsModel(
            redis_host=form_data.get("redis_host"),
            redis_port=int(form_data.get("redis_port")),
            redis_username=form_data.get("redis_username") or None,
            redis_password=new_redis_password if new_redis_password else current_settings.redis_password,
            rate_limit_requests=int(form_data.get("rate_limit_requests")),
            rate_limit_window_minutes=int(form_data.get("rate_limit_window_minutes")),
            allowed_ips=form_data.get("allowed_ips", ""),
            denied_ips=form_data.get("denied_ips", ""),
            model_update_interval_minutes=int(form_data.get("model_update_interval_minutes")),
        )
        
        await settings_crud.update_app_settings(db, settings_data=updated_settings_data)
        
        request.app.state.settings = updated_settings_data

        flash(request, "Settings updated successfully. Changes are now live.", "success")
    except Exception as e:
        logger.error(f"Failed to update settings: {e}")
        flash(request, f"Error updating settings: {e}", "error")

    return RedirectResponse(url=request.url_for("admin_settings"), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/users", name="create_new_user", dependencies=[Depends(validate_csrf_token)])
async def create_new_user(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user), username: str = Form(...), password: str = Form(...)):
    existing_user = await user_crud.get_user_by_username(db, username=username)
    if existing_user: flash(request, f"User '{username}' already exists.", "error")
    else:
        user_in = UserCreate(username=username, password=password)
        await user_crud.create_user(db, user=user_in)
        flash(request, f"User '{username}' created successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)

@router.get("/users/{user_id}", response_class=HTMLResponse, name="get_user_details")
async def get_user_details(request: Request, user_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    context = get_template_context(request)
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")
    context["user"] = user
    context["api_keys"] = await apikey_crud.get_api_keys_for_user(db, user_id=user_id)
    context["csrf_token"] = await get_csrf_token(request)
    return templates.TemplateResponse("admin/user_details.html", context)

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
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
    
    await user_crud.delete_user(db, user_id=user_id)
    flash(request, f"User '{user.username}' has been deleted.", "success")
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)