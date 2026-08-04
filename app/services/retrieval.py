"""RAG retrieval over the product vector store, with metadata filtering,
multi-query fusion, behavioural re-ranking and category diversification.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product
from . import vector_store

logger = logging.getLogger(__name__)


def _base_where(level: Optional[str] = None, max_price: Optional[float] = None) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = [{"is_published": {"$eq": True}}]
    if level:
        clauses.append({"level": {"$eq": level}})
    if max_price is not None:
        clauses.append({"price": {"$lte": float(max_price)}})
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def multi_query_search(
    queries: list[str],
    k: int = 12,
    *,
    level: Optional[str] = None,
    max_price: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Run every query and fuse results, keeping each product's best score.

    Products that surface for more than one query get a small consensus bonus —
    cheap reciprocal-rank-style fusion without a second model call.
    """
    store = vector_store.get_store()
    where = _base_where(level=level, max_price=max_price)
    fused: dict[str, dict[str, Any]] = {}

    for query in queries or []:
        if not query or not query.strip():
            continue
        try:
            hits = store.query(query, k=k, where=where)
        except Exception as exc:  # noqa: BLE001
            logger.error("Vector query failed for %r: %s", query, exc)
            continue
        for rank, hit in enumerate(hits):
            existing = fused.get(hit["id"])
            if existing is None:
                hit = dict(hit)
                hit["matched_queries"] = [query]
                hit["best_rank"] = rank
                fused[hit["id"]] = hit
            else:
                existing["matched_queries"].append(query)
                existing["score"] = max(existing["score"], hit["score"])
                existing["best_rank"] = min(existing["best_rank"], rank)

    results = list(fused.values())
    for hit in results:
        consensus = min(len(hit["matched_queries"]) - 1, 3) * 0.04
        hit["retrieval_score"] = hit["score"] + consensus
    results.sort(key=lambda h: h["retrieval_score"], reverse=True)
    return results


def rerank(
    hits: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    exclude_product_ids: Optional[set[int]] = None,
) -> list[dict[str, Any]]:
    """Blend semantic similarity with behavioural fit and catalog quality."""
    exclude_product_ids = exclude_product_ids or set()
    cat_weights = {c["name"]: c["weight"] for c in profile.get("top_categories", [])}
    max_cat = max(cat_weights.values(), default=1.0) or 1.0
    term_set = {t["name"].lower() for t in profile.get("top_terms", [])}
    level_names = [lv["name"] for lv in profile.get("top_levels", [])]
    preferred_level = level_names[0] if level_names else None

    ranked: list[dict[str, Any]] = []
    for hit in hits:
        meta = hit.get("metadata") or {}
        pid = meta.get("product_id")
        if pid in exclude_product_ids:
            continue

        semantic = float(hit.get("retrieval_score", hit.get("score", 0.0)))
        category_fit = (cat_weights.get(meta.get("category", ""), 0.0) / max_cat) if cat_weights else 0.0
        tags = {t.strip().lower() for t in str(meta.get("tags", "")).split(",") if t.strip()}
        tag_overlap = len(tags & term_set) / max(len(term_set), 1) if term_set else 0.0
        level_fit = 1.0 if preferred_level and meta.get("level") == preferred_level else 0.0
        quality = float(meta.get("rating", 0.0)) / 5.0

        final = (
            0.55 * semantic
            + 0.18 * category_fit
            + 0.12 * tag_overlap
            + 0.08 * level_fit
            + 0.07 * quality
        )
        hit = dict(hit)
        hit["final_score"] = round(final, 4)
        hit["score_breakdown"] = {
            "semantic": round(semantic, 4),
            "category_fit": round(category_fit, 4),
            "tag_overlap": round(tag_overlap, 4),
            "level_fit": level_fit,
            "quality": round(quality, 4),
        }
        ranked.append(hit)

    ranked.sort(key=lambda h: h["final_score"], reverse=True)
    return ranked


def diversify(hits: list[dict[str, Any]], limit: int, max_per_category: int = 2) -> list[dict[str, Any]]:
    """Avoid handing back four near-identical courses."""
    chosen: list[dict[str, Any]] = []
    per_category: dict[str, int] = {}
    for hit in hits:
        category = (hit.get("metadata") or {}).get("category", "")
        if per_category.get(category, 0) >= max_per_category:
            continue
        chosen.append(hit)
        per_category[category] = per_category.get(category, 0) + 1
        if len(chosen) >= limit:
            break
    if len(chosen) < limit:  # backfill if the diversity cap starved us
        for hit in hits:
            if hit not in chosen:
                chosen.append(hit)
            if len(chosen) >= limit:
                break
    return chosen


def quality_signal(hits: list[dict[str, Any]], threshold: float = 0.28) -> dict[str, Any]:
    """Cheap, deterministic retrieval grading used by the agent's grade node."""
    if not hits:
        return {"ok": False, "reason": "no documents retrieved", "top_score": 0.0, "strong": 0}
    top = float(hits[0].get("final_score", hits[0].get("score", 0.0)))
    strong = sum(1 for h in hits if float(h.get("final_score", h.get("score", 0.0))) >= threshold)
    if top < threshold:
        return {"ok": False, "reason": f"top score {top:.2f} below threshold", "top_score": top, "strong": strong}
    if strong < 2:
        return {"ok": False, "reason": "only one strong match", "top_score": top, "strong": strong}
    return {"ok": True, "reason": "sufficient relevant matches", "top_score": top, "strong": strong}


def hydrate(db: Session, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach live SQL rows to vector hits — the vector store may lag by seconds."""
    ids = [(h.get("metadata") or {}).get("product_id") for h in hits]
    ids = [int(i) for i in ids if i is not None]
    if not ids:
        return []
    rows = db.execute(select(Product).where(Product.id.in_(ids))).scalars().all()
    by_id = {p.id: p for p in rows}
    hydrated = []
    for hit in hits:
        pid = (hit.get("metadata") or {}).get("product_id")
        product = by_id.get(int(pid)) if pid is not None else None
        if product is None or not product.is_published:
            continue
        item = product.to_dict()
        item["retrieval"] = {
            "final_score": hit.get("final_score"),
            "breakdown": hit.get("score_breakdown"),
            "matched_queries": hit.get("matched_queries", []),
        }
        hydrated.append(item)
    return hydrated


def popular_fallback(db: Session, limit: int) -> list[dict[str, Any]]:
    """Last resort only — used when retrieval genuinely returns nothing."""
    rows = (
        db.execute(
            select(Product)
            .where(Product.is_published.is_(True))
            .order_by(Product.rating.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [p.to_dict() for p in rows]
