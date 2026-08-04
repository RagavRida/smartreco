"""Turn raw events into an interest profile the agent can reason over.

This is deliberately deterministic Python, not an LLM call: distilling 120 events
into a weighted profile is cheap arithmetic, and doing it here means the model
receives a compact, high-signal brief instead of a raw event dump.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Event, Product

# How much each kind of signal says about intent.
EVENT_WEIGHTS: dict[str, float] = {
    "search": 3.0,
    "product_view": 2.0,
    "click": 1.5,
    "add_to_cart": 5.0,
    "dwell": 1.0,
    "scroll_depth": 0.5,
    "page_view": 0.3,
}

# Signals meaningful enough to count toward "should we re-run the agent?"
SIGNIFICANT_EVENTS = {"search", "product_view", "click", "add_to_cart", "dwell"}

_HALF_LIFE_HOURS = 48.0


def _recency_weight(created_at: datetime, now: datetime) -> float:
    """Exponential decay — yesterday's obsession outranks last week's."""
    age_hours = max(0.0, (now - created_at).total_seconds() / 3600.0)
    return 0.5 ** (age_hours / _HALF_LIFE_HOURS)


def recent_events(db: Session, user_id: int, limit: Optional[int] = None) -> list[Event]:
    limit = limit or settings.behavior_window_events
    cutoff = datetime.utcnow() - timedelta(hours=settings.behavior_window_hours)
    stmt = (
        select(Event)
        .where(Event.user_id == user_id, Event.created_at >= cutoff)
        .order_by(Event.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def build_profile(db: Session, user_id: int, events: Optional[list[Event]] = None) -> dict[str, Any]:
    """Weighted interest profile: categories, levels, search terms, viewed products."""
    events = events if events is not None else recent_events(db, user_id)
    now = datetime.utcnow()

    categories: dict[str, float] = defaultdict(float)
    levels: dict[str, float] = defaultdict(float)
    terms: dict[str, float] = defaultdict(float)
    product_scores: dict[int, float] = defaultdict(float)
    dwell_by_product: dict[int, int] = defaultdict(int)
    searches: list[str] = []
    type_counts: dict[str, int] = defaultdict(int)

    for event in events:
        base = EVENT_WEIGHTS.get(event.event_type, 0.5)
        weight = base * _recency_weight(event.created_at or now, now)
        type_counts[event.event_type] += 1

        if event.category:
            categories[event.category] += weight
        if event.product_id:
            product_scores[event.product_id] += weight
            if event.dwell_ms:
                dwell_by_product[event.product_id] += event.dwell_ms
        if event.event_type == "search" and event.query:
            q = event.query.strip()
            if q:
                searches.append(q)
                for token in _keywords(q):
                    terms[token] += weight
        meta = event.meta
        if isinstance(meta, dict):
            level = meta.get("level")
            if isinstance(level, str) and level:
                levels[level] += weight
            for tag in meta.get("tags", []) or []:
                if isinstance(tag, str) and tag:
                    terms[tag.lower()] += weight * 0.6

        # Long dwell is a strong interest signal; upweight the product's tags.
        if event.dwell_ms and event.dwell_ms > 20000 and event.category:
            categories[event.category] += 0.5

    # Enrich with titles/tags of the products actually viewed.
    viewed_products: list[dict[str, Any]] = []
    if product_scores:
        rows = (
            db.execute(select(Product).where(Product.id.in_(list(product_scores.keys()))))
            .scalars()
            .all()
        )
        for product in rows:
            score = product_scores.get(product.id, 0.0)
            for tag in product.tag_list:
                terms[tag.lower()] += score * 0.4
            categories[product.category] += score * 0.3
            levels[product.level] += score * 0.3
            viewed_products.append(
                {
                    "id": product.id,
                    "title": product.title,
                    "category": product.category,
                    "level": product.level,
                    "score": round(score, 3),
                    "dwell_ms": dwell_by_product.get(product.id, 0),
                }
            )
        viewed_products.sort(key=lambda p: p["score"], reverse=True)

    top_categories = _top(categories, 4)
    top_terms = _top(terms, 10)
    top_levels = _top(levels, 2)

    total_dwell_ms = sum(e.dwell_ms or 0 for e in events)
    profile = {
        "event_count": len(events),
        "event_types": dict(type_counts),
        "top_categories": top_categories,
        "top_levels": top_levels,
        "top_terms": top_terms,
        "recent_searches": _dedupe(searches)[:8],
        "viewed_products": viewed_products[:8],
        "total_dwell_seconds": round(total_dwell_ms / 1000.0, 1),
        "engagement": _engagement_label(events, total_dwell_ms),
        "window_started": min((e.created_at for e in events), default=None),
        "window_ended": max((e.created_at for e in events), default=None),
    }
    profile["signature"] = signature(profile)
    profile["summary"] = summarize(profile)
    return profile


def _keywords(text: str) -> list[str]:
    import re

    stop = {"the", "a", "an", "for", "and", "to", "of", "in", "with", "how", "best", "course", "courses"}
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in stop and len(t) > 2]


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = item.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


def _top(scores: dict[str, float], n: int) -> list[dict[str, Any]]:
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return [{"name": k, "weight": round(v, 3)} for k, v in ranked if v > 0]


def _engagement_label(events: list[Event], total_dwell_ms: int) -> str:
    if len(events) >= 25 or total_dwell_ms > 300_000:
        return "highly engaged"
    if len(events) >= 8:
        return "actively exploring"
    if events:
        return "just getting started"
    return "no activity yet"


def signature(profile: dict[str, Any]) -> str:
    """Stable hash of *what the user is into* — not of every event.

    Two profiles with the same signature would produce the same recommendation,
    so the trigger policy can skip the LLM call entirely.
    """
    parts = [c["name"] for c in profile.get("top_categories", [])]
    parts += [t["name"] for t in profile.get("top_terms", [])[:6]]
    parts += [str(p["id"]) for p in profile.get("viewed_products", [])[:5]]
    parts += [s.lower() for s in profile.get("recent_searches", [])[:4]]
    raw = "|".join(parts) or "empty"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def summarize(profile: dict[str, Any]) -> str:
    """One-paragraph natural-language brief handed to the agent."""
    if not profile.get("event_count"):
        return "This user has no tracked activity yet."
    cats = ", ".join(c["name"] for c in profile.get("top_categories", [])) or "no clear category"
    terms = ", ".join(t["name"] for t in profile.get("top_terms", [])[:6]) or "none"
    searches = "; ".join(profile.get("recent_searches", [])) or "none"
    viewed = "; ".join(
        f"{p['title']} ({round(p['dwell_ms'] / 1000)}s)" for p in profile.get("viewed_products", [])[:5]
    ) or "none"
    levels = ", ".join(lv["name"] for lv in profile.get("top_levels", [])) or "unspecified"
    return (
        f"Across {profile['event_count']} tracked actions the user is {profile['engagement']}. "
        f"Dominant interest areas: {cats}. Recurring topics: {terms}. "
        f"Searches performed: {searches}. Products they lingered on: {viewed}. "
        f"Preferred level signal: {levels}. "
        f"Total attention time: {profile['total_dwell_seconds']}s."
    )


def retrieval_queries(profile: dict[str, Any]) -> list[str]:
    """Deterministic query seeds — used as-is when we skip the LLM analyze step."""
    queries: list[str] = []
    queries.extend(profile.get("recent_searches", [])[:3])
    cats = [c["name"] for c in profile.get("top_categories", [])[:2]]
    terms = [t["name"] for t in profile.get("top_terms", [])[:4]]
    if cats:
        queries.append(" ".join(cats + terms[:3]))
    for product in profile.get("viewed_products", [])[:2]:
        queries.append(f"{product['title']} {product['category']}")
    if not queries:
        queries = ["popular highly rated courses for beginners"]
    return _dedupe(queries)[:4]
