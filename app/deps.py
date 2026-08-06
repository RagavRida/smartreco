"""Shared FastAPI dependencies: current user, role guards, template env."""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .config import BASE_DIR, settings
from .database import get_db
from .models import User
from .security import SESSION_COOKIE, read_session

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals["app_name"] = settings.app_name


class RedirectToLogin(Exception):
    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> Optional[User]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = read_session(token)
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_user(
    request: Request, user: Optional[User] = Depends(get_current_user)
) -> User:
    if user is None:
        raise RedirectToLogin(str(request.url.path))
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def require_api_user(user: Optional[User] = Depends(get_current_user)) -> User:
    """API variant: 401 JSON instead of a redirect."""
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


def render(
    request: Request,
    template: str,
    context: Optional[dict] = None,
    status_code: int = 200,
    **kwargs,
):
    import os
    payload = {"request": request}
    payload.update(context or {})
    payload.update(kwargs)
    payload.setdefault("current_user", None)
    payload.setdefault("show_debug_tracking", os.environ.get("SHOW_DEBUG_TRACKING", "").strip().lower() in ("1", "true", "yes", "on"))
    return templates.TemplateResponse(request, template, payload, status_code=status_code)


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status_code)
