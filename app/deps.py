"""Shared FastAPI dependencies: current user, role guards, template env."""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .config import BASE_DIR, settings
from .database import get_db
from .models import User
from .security import SESSION_COOKIE, decode_token, read_session

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals["app_name"] = settings.app_name


class RedirectToLogin(Exception):
    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


# ---------------------------------------------------------------------------
# Cookie-based auth (for browser-rendered HTML pages)
# ---------------------------------------------------------------------------

def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> Optional[User]:
    """Read the session cookie and return the user, or None."""
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
    """Require a logged-in user (cookie). Redirects to /login for HTML pages."""
    if user is None:
        raise RedirectToLogin(str(request.url.path))
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    """Require an admin user (cookie). 403 if not admin."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


# ---------------------------------------------------------------------------
# JWT-based auth (for programmatic API access)
# ---------------------------------------------------------------------------

# Optional bearer — does not raise if header is missing (allows dual-auth)
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user_jwt(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Decode a JWT Bearer token and return the user, or None."""
    if credentials is None:
        return None
    payload = decode_token(credentials.credentials)
    if payload is None:
        return None
    if payload.get("type") != "access":
        return None
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def get_api_user(
    request: Request,
    jwt_user: Optional[User] = Depends(get_current_user_jwt),
    cookie_user: Optional[User] = Depends(get_current_user),
) -> User:
    """Dual-auth: tries JWT first, then falls back to session cookie.
    Returns 401 if neither method provides a valid user."""
    user = jwt_user or cookie_user
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide a Bearer token or session cookie.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_role(*allowed_roles: str) -> Callable:
    """Factory that returns a dependency checking the user's role against a whitelist."""
    def _guard(user: User = Depends(get_api_user)) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {', '.join(allowed_roles)}",
            )
        return user
    return _guard


# Convenience shorthands
require_user_jwt = require_role("user", "admin")
require_admin_jwt = require_role("admin")


def require_api_user(user: Optional[User] = Depends(get_current_user)) -> User:
    """API variant (legacy): 401 JSON instead of a redirect."""
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


# ---------------------------------------------------------------------------
# Template rendering helpers
# ---------------------------------------------------------------------------

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
