# app/api/v1/routes/admin.py
import logging
from typing import Union, Optional

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import verify_password, get_password_hash
from app.database.session import get_db
from app.database.models import User
from app.crud import user_crud, apikey_crud, log_crud, server_crud
from app.schema.user import UserCreate
from app.schema.server import ServerCreate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# ... (helpers and other routes remain the same up to API Key Management) ...
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
async def admin_login_form(request: Request): return templates.TemplateResponse("admin/login.html", {"request": request})
@router.post("/login", name="admin_login_post")
async def admin_login_post(request: Request, db: AsyncSession = Depends(get_db), username: str = Form(...), password: str = Form(...)):
    user = await user_crud.get_user_by_username(db, username=username)
    if not user or not user.is_admin or not verify_password(password, user.hashed_password):
        flash(request, "Invalid username or password", "error")
        return RedirectResponse(url=request.url_for("admin_login"), status_code=status.HTTP_303_SEE_OTHER)
    request.session["user_id"] = user.id
    flash(request, "Successfully logged in.", "success")
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
@router.get("/logout", name="admin_logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=request.url_for("admin_login"), status_code=status.HTTP_303_SEE_OTHER)
@router.get("/dashboard", response_class=HTMLResponse, name="admin_dashboard")
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    users = await user_crud.get_users(db)
    return templates.TemplateResponse("admin/dashboard.html", {"request": request, "users": users})
@router.get("/stats", response_class=HTMLResponse, name="admin_stats")
async def admin_stats(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    key_usage_stats = await log_crud.get_usage_statistics(db)
    daily_stats = await log_crud.get_daily_usage_stats(db, days=30)
    hourly_stats = await log_crud.get_hourly_usage_stats(db)
    server_stats = await log_crud.get_server_load_stats(db)
    daily_labels = [row.date.strftime('%Y-%m-%d') for row in daily_stats]; daily_data = [row.request_count for row in daily_stats]
    hourly_labels = [row['hour'] for row in hourly_stats]; hourly_data = [row['request_count'] for row in hourly_stats]
    server_labels = [row.server_name for row in server_stats]; server_data = [row.request_count for row in server_stats]
    return templates.TemplateResponse("admin/statistics.html", {"request": request, "key_usage_stats": key_usage_stats, "daily_labels": daily_labels, "daily_data": daily_data, "hourly_labels": hourly_labels, "hourly_data": hourly_data, "server_labels": server_labels, "server_data": server_data})
@router.get("/help", response_class=HTMLResponse, name="admin_help")
async def admin_help_page(request: Request, admin_user: User = Depends(require_admin_user)): return templates.TemplateResponse("admin/help.html", {"request": request})
@router.get("/servers", response_class=HTMLResponse, name="admin_servers")
async def admin_server_management(request: Request, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    servers = await server_crud.get_servers(db)
    return templates.TemplateResponse("admin/servers.html", {"request": request, "servers": servers})
@router.post("/servers/add", name="admin_add_server")
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
@router.post("/servers/{server_id}/delete", name="admin_delete_server")
async def admin_delete_server(request: Request, server_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    await server_crud.delete_server(db, server_id=server_id)
    flash(request, "Server deleted successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)
@router.post("/users", name="create_new_user")
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
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")
    api_keys = await apikey_crud.get_api_keys_for_user(db, user_id=user_id)
    return templates.TemplateResponse("admin/user_details.html", {"request": request, "user": user, "api_keys": api_keys})

# ----------------------------------------------------------------------
# API KEY MANAGEMENT (REVISED)
# ----------------------------------------------------------------------
@router.post("/users/{user_id}/keys/create", name="admin_create_key")
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
    
    request.state.user = await db.get(User, current_admin_id)
    return templates.TemplateResponse(
        "admin/key_created.html",
        {"request": request, "plain_key": plain_key, "user_id": user_id},
    )

# --- NEW ROUTE ---
@router.post("/keys/{key_id}/toggle-active", name="admin_toggle_key_active")
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
# --- END NEW ROUTE ---

@router.post("/keys/{key_id}/revoke", name="admin_revoke_key")
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

@router.post("/users/{user_id}/delete", name="delete_user_account")
async def delete_user_account(request: Request, user_id: int, db: AsyncSession = Depends(get_db), admin_user: User = Depends(require_admin_user)):
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        flash(request, "Cannot delete an admin account.", "error")
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
    
    await user_crud.delete_user(db, user_id=user_id)
    flash(request, f"User '{user.username}' has been deleted.", "success")
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)