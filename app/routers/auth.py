"""Email/password authentication + JWT token endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_api_user, get_current_user, redirect, render
from ..models import User
from ..security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    make_session,
    verify_password,
)

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Browser HTML form auth (cookie-based — unchanged)
# ---------------------------------------------------------------------------

@router.get("/login")
def login_page(request: Request, user=Depends(get_current_user)):
    if user:
        return redirect("/dashboard")
    return render(request, "login.html", {"next": request.query_params.get("next", "/dashboard")})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/dashboard"),
    db: Session = Depends(get_db),
):
    normalized = email.strip().lower()
    user = db.execute(select(User).where(func.lower(User.email) == normalized)).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash) or not user.is_active:
        return render(
            request,
            "login.html",
            {"error": "That email and password combination did not work.", "email": email, "next": next},
        )
    destination = next if next.startswith("/") else "/dashboard"
    if user.is_admin and destination == "/dashboard":
        destination = "/admin/products"
    response = redirect(destination)
    response.set_cookie(
        SESSION_COOKIE,
        make_session(user.id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/register")
def register_page(request: Request, user=Depends(get_current_user)):
    if user:
        return redirect("/dashboard")
    return render(request, "register.html", {})


@router.post("/register")
def register(
    request: Request,
    name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized = email.strip().lower()
    if len(password) < 6:
        return render(
            request, "register.html", {"error": "Password must be at least 6 characters.", "email": email, "name": name}
        )
    existing = db.execute(select(User).where(func.lower(User.email) == normalized)).scalar_one_or_none()
    if existing:
        return render(
            request, "register.html", {"error": "An account with that email already exists.", "email": email, "name": name}
        )

    user = User(
        email=normalized,
        name=name.strip(),
        password_hash=hash_password(password),
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    response = redirect("/dashboard")
    response.set_cookie(
        SESSION_COOKIE, make_session(user.id), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax"
    )
    return response


@router.get("/logout")
@router.post("/logout")
def logout():
    response = redirect("/")
    response.delete_cookie(SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# JWT token endpoints (for programmatic / API access)
# ---------------------------------------------------------------------------

class TokenRequest(BaseModel):
    """JSON body for the token endpoint."""
    email: str
    password: str


class TokenResponse(BaseModel):
    """JWT token pair returned on successful authentication."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str
    user_id: int
    name: str


class RefreshRequest(BaseModel):
    """JSON body for the refresh endpoint."""
    refresh_token: str


class RefreshResponse(BaseModel):
    """New access token returned on successful refresh."""
    access_token: str
    token_type: str = "bearer"


@router.post("/api/auth/token", response_model=TokenResponse, tags=["jwt"])
def api_login(body: TokenRequest, db: Session = Depends(get_db)):
    """Authenticate with email + password and receive JWT access & refresh tokens."""
    normalized = body.email.strip().lower()
    user = db.execute(
        select(User).where(func.lower(User.email) == normalized)
    ).scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash) or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
        role=user.role,
        user_id=user.id,
        name=user.name,
    )


@router.post("/api/auth/refresh", response_model=RefreshResponse, tags=["jwt"])
def api_refresh(body: RefreshRequest, db: Session = Depends(get_db)):
    """Exchange a valid refresh token for a new access token."""
    payload = decode_token(body.refresh_token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
        )
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not a refresh token.",
        )
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token payload.",
        )

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated.",
        )

    return RefreshResponse(
        access_token=create_access_token(user.id, user.role),
    )


@router.get("/api/auth/me", tags=["jwt"])
def api_me(user: User = Depends(get_api_user)):
    """Return the current authenticated user's profile."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }

