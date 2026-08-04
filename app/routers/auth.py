"""Email/password authentication."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, redirect, render
from ..models import User
from ..security import SESSION_COOKIE, SESSION_MAX_AGE, hash_password, make_session, verify_password

router = APIRouter(tags=["auth"])


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
