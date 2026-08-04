"""Product CRUD with dual-write to SQL + the vector database.

Contract: SQL is the source of truth, the vector store is a derived mirror.
Every mutation goes through here, and every row carries vector_synced /
vector_version so drift is visible and repairable (see reconcile()).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product
from . import vector_store

logger = logging.getLogger(__name__)

MUTABLE_FIELDS = {
    "title", "description", "category", "level", "tags", "price",
    "duration_hours", "instructor", "rating", "image_url", "is_published",
}


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "product"


def _unique_slug(db: Session, title: str, exclude_id: Optional[int] = None) -> str:
    base = slugify(title)
    slug, n = base, 2
    while True:
        stmt = select(Product).where(Product.slug == slug)
        if exclude_id:
            stmt = stmt.where(Product.id != exclude_id)
        if db.execute(stmt).scalar_one_or_none() is None:
            return slug
        slug = f"{base}-{n}"
        n += 1


def _sync_to_vector(db: Session, product: Product) -> None:
    """Mirror a product into the vector store. Failures are recorded, not swallowed."""
    store = vector_store.get_store()
    doc_id = vector_store.product_doc_id(product.id)
    try:
        if product.is_published:
            store.upsert(doc_id, product.embedding_text(), product.vector_metadata())
        else:
            # Unpublished products must not be retrievable.
            store.delete(doc_id)
        product.vector_synced = True
        product.vector_version = (product.vector_version or 0) + 1
        product.vector_error = ""
    except Exception as exc:  # noqa: BLE001
        product.vector_synced = False
        product.vector_error = str(exc)[:500]
        logger.error("Vector sync failed for product %s: %s", product.id, exc)
    db.add(product)
    db.commit()


def create_product(db: Session, data: dict[str, Any]) -> Product:
    payload = {k: v for k, v in data.items() if k in MUTABLE_FIELDS}
    product = Product(**payload)
    product.slug = _unique_slug(db, product.title)
    db.add(product)
    db.commit()          # 1) SQL write — source of truth, gets us the id
    db.refresh(product)
    _sync_to_vector(db, product)  # 2) vector write — derived mirror
    return product


def update_product(db: Session, product: Product, data: dict[str, Any]) -> Product:
    retitled = "title" in data and data["title"] != product.title
    for key, value in data.items():
        if key in MUTABLE_FIELDS:
            setattr(product, key, value)
    if retitled:
        product.slug = _unique_slug(db, product.title, exclude_id=product.id)
    product.vector_synced = False  # mark stale until the mirror catches up
    db.add(product)
    db.commit()
    db.refresh(product)
    _sync_to_vector(db, product)
    return product


def delete_product(db: Session, product: Product) -> None:
    doc_id = vector_store.product_doc_id(product.id)
    # Delete the mirror first: an orphaned vector would surface a product that no
    # longer exists, which is worse than a briefly missing one.
    try:
        vector_store.get_store().delete(doc_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Vector delete failed for product %s: %s", product.id, exc)
    db.delete(product)
    db.commit()


def reconcile(db: Session) -> dict[str, int]:
    """Repair drift between SQL and the vector store. Safe to run repeatedly."""
    store = vector_store.get_store()
    products: Iterable[Product] = db.execute(select(Product)).scalars().all()
    repaired = 0
    removed = 0
    live_ids = set()

    for product in products:
        doc_id = vector_store.product_doc_id(product.id)
        if product.is_published:
            live_ids.add(doc_id)
        if not product.vector_synced or not product.is_published:
            _sync_to_vector(db, product)
            repaired += 1

    for stale_id in store.all_ids() - live_ids:
        try:
            store.delete(stale_id)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not remove stale vector %s: %s", stale_id, exc)

    return {"repaired": repaired, "removed_orphans": removed, "vector_count": store.count()}


def sync_status(db: Session) -> dict[str, Any]:
    store = vector_store.get_store()
    total = db.execute(select(Product)).scalars().all()
    published = [p for p in total if p.is_published]
    unsynced = [p for p in total if not p.vector_synced]
    return {
        "backend": store.backend,
        "sql_products": len(total),
        "sql_published": len(published),
        "vector_documents": store.count(),
        "unsynced": len(unsynced),
        "in_sync": len(unsynced) == 0 and store.count() == len(published),
    }
