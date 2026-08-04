"""Admin: product CRUD (dual-written to SQL + vector DB) and system health."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import redirect, render, require_admin
from ..models import Event, Product, Recommendation, User
from ..services import mailer, mesh_client, product_service, scheduler, vector_store
from ..services.embeddings import using_fallback
from ..agent.graph import engine_name

router = APIRouter(prefix="/admin", tags=["admin"])


def _form_to_data(
    title: str, description: str, category: str, level: str, tags: str,
    price: float, duration_hours: float, instructor: str, rating: float,
    image_url: str, is_published: bool,
) -> dict:
    return {
        "title": title.strip(),
        "description": description.strip(),
        "category": category.strip().lower() or "general",
        "level": level.strip().lower() or "beginner",
        "tags": ",".join(t.strip().lower() for t in tags.split(",") if t.strip()),
        "price": float(price or 0),
        "duration_hours": float(duration_hours or 0),
        "instructor": instructor.strip(),
        "rating": max(0.0, min(float(rating or 0), 5.0)),
        "image_url": image_url.strip(),
        "is_published": bool(is_published),
    }


@router.get("/products")
def list_products(request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    products = list(db.execute(select(Product).order_by(Product.updated_at.desc())).scalars().all())
    return render(
        request,
        "admin_products.html",
        {
            "current_user": admin,
            "products": products,
            "sync": product_service.sync_status(db),
            "flash": request.query_params.get("flash", ""),
        },
    )


@router.get("/products/new")
def new_product_form(request: Request, admin: User = Depends(require_admin)):
    return render(request, "admin_product_form.html", {"current_user": admin, "product": None})


@router.post("/products/new")
def create_product(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form("general"),
    level: str = Form("beginner"),
    tags: str = Form(""),
    price: float = Form(0.0),
    duration_hours: float = Form(0.0),
    instructor: str = Form(""),
    rating: float = Form(4.5),
    image_url: str = Form(""),
    is_published: bool = Form(False),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = _form_to_data(title, description, category, level, tags, price,
                         duration_hours, instructor, rating, image_url, is_published)
    product = product_service.create_product(db, data)
    flash = f"Created “{product.title}” — vector sync {'ok' if product.vector_synced else 'FAILED'}"
    return redirect(f"/admin/products?flash={flash}")


@router.get("/products/{product_id}/edit")
def edit_product_form(
    product_id: int, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    product = db.get(Product, product_id)
    if product is None:
        return redirect("/admin/products?flash=Product not found")
    return render(request, "admin_product_form.html", {"current_user": admin, "product": product})


@router.post("/products/{product_id}/edit")
def update_product(
    product_id: int,
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form("general"),
    level: str = Form("beginner"),
    tags: str = Form(""),
    price: float = Form(0.0),
    duration_hours: float = Form(0.0),
    instructor: str = Form(""),
    rating: float = Form(4.5),
    image_url: str = Form(""),
    is_published: bool = Form(False),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    product = db.get(Product, product_id)
    if product is None:
        return redirect("/admin/products?flash=Product not found")
    data = _form_to_data(title, description, category, level, tags, price,
                         duration_hours, instructor, rating, image_url, is_published)
    product = product_service.update_product(db, product, data)
    flash = f"Updated “{product.title}” — vector v{product.vector_version}"
    return redirect(f"/admin/products?flash={flash}")


@router.post("/products/{product_id}/delete")
def delete_product(product_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    product = db.get(Product, product_id)
    if product is None:
        return redirect("/admin/products?flash=Product not found")
    title = product.title
    product_service.delete_product(db, product)
    return redirect(f"/admin/products?flash=Deleted “{title}” from SQL and the vector store")


@router.post("/reconcile")
def reconcile(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    report = product_service.reconcile(db)
    flash = (
        f"Reconciled — repaired {report['repaired']}, removed {report['removed_orphans']} orphans, "
        f"{report['vector_count']} vectors live"
    )
    return redirect(f"/admin/products?flash={flash}")


@router.get("/system")
def system(request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    counts = {
        "users": db.execute(select(func.count(User.id))).scalar_one(),
        "products": db.execute(select(func.count(Product.id))).scalar_one(),
        "events": db.execute(select(func.count(Event.id))).scalar_one(),
        "recommendations": db.execute(select(func.count(Recommendation.id))).scalar_one(),
    }
    return render(
        request,
        "admin_system.html",
        {
            "current_user": admin,
            "counts": counts,
            "sync": product_service.sync_status(db),
            "vector_backend": vector_store.get_store().backend,
            "agent_engine": engine_name(),
            "embeddings_fallback": using_fallback(),
            "scheduler": scheduler.status(),
            "smtp_configured": mailer.smtp_configured(),
            "mesh_health": request.query_params.get("mesh") == "1" and mesh_client.health() or None,
            "flash": request.query_params.get("flash", ""),
        },
    )


@router.post("/digest/run")
def run_digest_now(request: Request, admin: User = Depends(require_admin)):
    """Fire the scheduled job manually — useful for demoing proactive delivery."""
    base = str(request.base_url).rstrip("/")
    report = scheduler.run_daily_digest(base_url=base)
    return redirect(f"/admin/system?flash=Digest job processed {report['processed']} user(s)")
