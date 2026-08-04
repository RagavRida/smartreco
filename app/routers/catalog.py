"""Storefront: home, catalog, search, product detail, dashboard."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, redirect, render, require_user
from ..models import Product, User
from ..services import behavior, recommender, retrieval, trigger

router = APIRouter(tags=["catalog"])


def _categories(db: Session) -> list[str]:
    rows = db.execute(
        select(Product.category).where(Product.is_published.is_(True)).distinct()
    ).scalars().all()
    return sorted({r for r in rows if r})


@router.get("/")
def home(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)):
    featured = (
        db.execute(
            select(Product)
            .where(Product.is_published.is_(True))
            .order_by(Product.rating.desc())
            .limit(6)
        )
        .scalars()
        .all()
    )
    return render(
        request,
        "home.html",
        {"current_user": user, "featured": featured, "categories": _categories(db)},
    )


@router.get("/catalog")
def catalog(
    request: Request,
    category: str = Query(""),
    level: str = Query(""),
    q: str = Query(""),
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user),
):
    stmt = select(Product).where(Product.is_published.is_(True))
    if category:
        stmt = stmt.where(Product.category == category)
    if level:
        stmt = stmt.where(Product.level == level)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Product.title.ilike(like), Product.description.ilike(like), Product.tags.ilike(like))
        )
    products = list(db.execute(stmt.order_by(Product.rating.desc())).scalars().all())

    # Semantic assist: when keyword search is thin, surface vector matches too.
    semantic: list[dict] = []
    if q and len(products) < 4:
        hits = retrieval.multi_query_search([q], k=6)
        found_ids = {p.id for p in products}
        semantic = [
            item for item in retrieval.hydrate(db, hits) if item["id"] not in found_ids
        ][:4]

    return render(
        request,
        "catalog.html",
        {
            "current_user": user,
            "products": products,
            "semantic": semantic,
            "categories": _categories(db),
            "selected_category": category,
            "selected_level": level,
            "query": q,
        },
    )


@router.get("/product/{slug}")
def product_detail(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user),
):
    product = db.execute(select(Product).where(Product.slug == slug)).scalar_one_or_none()
    if product is None or not product.is_published:
        return render(request, "404.html", {"current_user": user}, status_code=404)

    related_hits = retrieval.multi_query_search([product.embedding_text()], k=6)
    related = [item for item in retrieval.hydrate(db, related_hits) if item["id"] != product.id][:3]

    return render(
        request,
        "product.html",
        {"current_user": user, "product": product, "related": related},
    )


@router.get("/dashboard")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    if user.is_admin:
        return redirect("/admin/products")

    result = recommender.get_recommendation(db, user)
    state = trigger.get_or_create_state(db, user.id)
    recent = behavior.recent_events(db, user.id, limit=12)

    return render(
        request,
        "dashboard.html",
        {
            "current_user": user,
            "recommendation": result.recommendation,
            "decision": result.decision,
            "ran_agent": result.ran_agent,
            "agent_state": state,
            "recent_events": recent,
        },
    )
