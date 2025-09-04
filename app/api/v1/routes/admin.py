import logging
from typing import Union

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.config import settings
from app.core.security import verify_password, get_password_hash
from app.database.session import get_db
from app.database.models import User
from app.crud import user_crud, apikey_crud, log_crud
from app.schema.user import UserCreate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Simple flash messaging
def flash(request: Request, message: str, category: str = "info"):
    if "_messages" not in request.session:
        request.session["_messages"] = []
    request.session["_messages"].append({"message": message, "category": category})

def get_flashed_messages(request: Request):
    return request.session.pop("_messages", [])

templates.env.globals["get_flashed_messages"] = get_flashed_messages


# --- Admin Authentication Dependencies ---
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
    """
    Dependency that ensures a user is logged in and is an admin.
    If not, it raises an HTTPException that triggers a redirect to the login page.
    This is the sole point of enforcement for all protected admin routes.
    """
    if not current_user or not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Not authorized",
            headers={"Location": str(request.url_for("admin_login"))},
        )
    # Attach user to request state for use in templates (`request.state.user`)
    request.state.user = current_user
    return current_user


# --- Admin Routes ---
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

@router.post("/users/{user_id}/keys", name="create_user_api_key")
async def create_user_api_key(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin_user),
    key_name: str = Form(...),
):
    plain_key, _ = await apikey_crud.create_api_key(db, user_id=user_id, key_name=key_name)
    flash(request, f"New API key created: {plain_key}. This is the only time you will see the full key.", "success")
    return RedirectResponse(url=request.url_for("get_user_details", user_id=user_id), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/keys/{key_id}/revoke", name="revoke_user_api_key")
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