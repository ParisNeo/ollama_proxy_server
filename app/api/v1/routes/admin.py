# app/api/v1/routes/admin.py
import logging
from typing import Union

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

# ----------------------------------------------------------------------
# Flash‑message helpers (unchanged)
# ----------------------------------------------------------------------
def flash(request: Request, message: str, category: str = "info"):
    if "_messages" not in request.session:
        request.session["_messages"] = []
    request.session["_messages"].append({"message": message, "category": category})

def get_flashed_messages(request: Request):
    return request.session.pop("_messages", [])

templates.env.globals["get_flashed_messages"] = get_flashed_messages

# ----------------------------------------------------------------------
# Admin authentication helpers (unchanged)
# ----------------------------------------------------------------------
async def get_current_user_from_cookie(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User | None:
    user_id = request.session.get("user_id")
    if user_id:
        user = await user_crud.get_user_by_id(db, user_id=user_id)
        return user
    return None

async def require_admin_user(
    request: Request, current_user: Union[User, None] = Depends(get_current_user_from_cookie)
) -> User:
    if not current_user or not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Not authorized",
            headers={"Location": str(request.url_for("admin_login"))},
        )
    request.state.user = current_user
    return current_user

# ----------------------------------------------------------------------
# Admin UI routes (unchanged up to the key‑creation endpoint)
# ----------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse, name="admin_login")
async def admin_login_form(request: Request):
    return templates.TemplateResponse("admin/login.html", {"request": request})

@router.post("/login", name="admin_login_post")
async def admin_login_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
):
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
async def admin_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
):
    users = await user_crud.get_users(db)
    return templates.TemplateResponse("admin/dashboard.html", {"request": request, "users": users})

@router.get("/stats", response_class=HTMLResponse, name="admin_stats")
async def admin_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
):
    stats = await log_crud.get_usage_statistics(db)
    return templates.TemplateResponse("admin/statistics.html", {"request": request, "stats": stats})

# ----------------------------------------------------------------------
# Server management routes (unchanged)
# ----------------------------------------------------------------------
@router.get("/servers", response_class=HTMLResponse, name="admin_servers")
async def admin_server_management(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
):
    servers = await server_crud.get_servers(db)
    return templates.TemplateResponse("admin/servers.html", {"request": request, "servers": servers})

@router.post("/servers/add", name="admin_add_server")
async def admin_add_server(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    server_name: str = Form(...),
    server_url: str = Form(...),
):
    existing_server = await server_crud.get_server_by_url(db, url=server_url)
    if existing_server:
        flash(request, f"Server with URL '{server_url}' already exists.", "error")
    else:
        try:
            server_in = ServerCreate(name=server_name, url=server_url)
            await server_crud.create_server(db, server=server_in)
            flash(request, f"Server '{server_name}' added successfully.", "success")
        except Exception:
            flash(request, "Invalid URL format.", "error")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)

@router.post("/servers/{server_id}/delete", name="admin_delete_server")
async def admin_delete_server(
    request: Request,
    server_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
):
    await server_crud.delete_server(db, server_id=server_id)
    flash(request, "Server deleted successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_servers"), status_code=status.HTTP_303_SEE_OTHER)

# ----------------------------------------------------------------------
# USER MANAGEMENT (unchanged up to API‑key creation)
# ----------------------------------------------------------------------
@router.post("/users", name="create_new_user")
async def create_new_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    username: str = Form(...),
    password: str = Form(...),
):
    existing_user = await user_crud.get_user_by_username(db, username=username)
    if existing_user:
        flash(request, f"User '{username}' already exists.", "error")
    else:
        user_in = UserCreate(username=username, password=password)
        await user_crud.create_user(db, user=user_in)
        flash(request, f"User '{username}' created successfully.", "success")
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)

@router.get("/users/{user_id}", response_class=HTMLResponse, name="get_user_details")
async def get_user_details(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
):
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    api_keys = await apikey_crud.get_api_keys_for_user(db, user_id=user_id)
    return templates.TemplateResponse("admin/user_details.html", {"request": request, "user": user, "api_keys": api_keys})

# ----------------------------------------------------------------------
# *** NEW: Show the generated key with a copy button ***
# ----------------------------------------------------------------------
@router.post("/users/{user_id}/keys", name="create_user_api_key")
async def create_user_api_key(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    key_name: str = Form(...),
):
    """
    Creates a new API key for the given user.
    Instead of flashing the key (which would be lost on a redirect),
    we render a dedicated page that displays the **full plain key**
    exactly once and offers a “Copy to clipboard” button.
    """
    plain_key, _ = await apikey_crud.create_api_key(db, user_id=user_id, key_name=key_name)

    # Render a page that shows the key and a copy button.
    return templates.TemplateResponse(
        "admin/key_created.html",
        {
            "request": request,
            "plain_key": plain_key,
            "user_id": user_id,
        },
    )

@router.post("/users/{user_id}/keys", name="create_user_api_key")
async def create_user_api_key(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    key_name: str = Form(...),
):
    """
    Creates a new API key for the given user.
    Instead of flashing the key (which would be lost on a redirect),
    we render a dedicated page that displays the **full plain key**
    exactly once and offers a “Copy to clipboard” button.
    """
    plain_key, _ = await apikey_crud.create_api_key(db, user_id=user_id, key_name=key_name)

    # --- FIX ---
    # The user object in request.state (admin_user) is from a different,
    # now-closed session. Accessing its properties in the template would
    # cause a lazy-load, which fails in a synchronous context.
    # We fetch a fresh user object using the current session ('db') and
    # update the request state to ensure the template renders correctly.
    fresh_admin_user = await user_crud.get_user_by_id(db, user_id=admin_user.id)
    request.state.user = fresh_admin_user
    # --- END FIX ---

    # Render a page that shows the key and a copy button.
    return templates.TemplateResponse(
        "admin/key_created.html",
        {
            "request": request,
            "plain_key": plain_key,
            "user_id": user_id,
        },
    )
@router.post("/users/{user_id}/delete", name="delete_user_account")
async def delete_user_account(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
):
    user = await user_crud.get_user_by_id(db, user_id=user_id)
    if not user:
         raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        flash(request, "Cannot delete an admin account.", "error")
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
    
    await user_crud.delete_user(db, user_id=user_id)
    flash(request, f"User '{user.username}' has been deleted.", "success")
    return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=status.HTTP_303_SEE_OTHER)
