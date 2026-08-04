"""Database schema.

users ─┬─< events            (raw behavioural signal)
       ├─< recommendations   (agent output, refreshed as behaviour changes)
       └─< agent_state       (1:1 bookkeeping so we never run the agent wastefully)

products  <── referenced by events (product_id) and by recommendation items (JSON payload)
          and mirrored into the vector store (vector_synced / vector_version)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user", index=True)  # user | admin
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_opt_in: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    events: Mapped[list["Event"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    agent_state: Mapped[Optional["AgentState"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Product(Base):
    """A course / product in the catalog. Dual-written to SQL + the vector store."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="general", index=True)
    level: Mapped[str] = mapped_column(String(40), default="beginner", index=True)
    tags: Mapped[str] = mapped_column(String(500), default="")  # comma separated
    price: Mapped[float] = mapped_column(Float, default=0.0)
    duration_hours: Mapped[float] = mapped_column(Float, default=0.0)
    instructor: Mapped[str] = mapped_column(String(120), default="")
    rating: Mapped[float] = mapped_column(Float, default=4.5)
    image_url: Mapped[str] = mapped_column(String(500), default="")
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # --- dual-write bookkeeping ---
    vector_synced: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    vector_version: Mapped[int] = mapped_column(Integer, default=0)
    vector_error: Mapped[str] = mapped_column(String(500), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]

    def embedding_text(self) -> str:
        """The document that gets embedded into the vector store."""
        parts = [
            f"{self.title}.",
            f"Category: {self.category}. Level: {self.level}.",
            f"Topics: {', '.join(self.tag_list)}." if self.tag_list else "",
            self.description or "",
        ]
        return " ".join(p for p in parts if p).strip()

    def vector_metadata(self) -> dict[str, Any]:
        return {
            "product_id": self.id,
            "title": self.title,
            "slug": self.slug,
            "category": self.category,
            "level": self.level,
            "tags": ",".join(self.tag_list),
            "price": float(self.price or 0.0),
            "rating": float(self.rating or 0.0),
            "duration_hours": float(self.duration_hours or 0.0),
            "is_published": bool(self.is_published),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "slug": self.slug,
            "description": self.description,
            "category": self.category,
            "level": self.level,
            "tags": self.tag_list,
            "price": self.price,
            "duration_hours": self.duration_hours,
            "instructor": self.instructor,
            "rating": self.rating,
            "image_url": self.image_url,
        }


class Event(Base):
    """One tracked behavioural signal. Written in bulk, never one-at-a-time."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_user_created", "user_id", "created_at"),
        Index("ix_events_user_type", "user_id", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    anon_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)

    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # page_view | product_view | search | click | dwell | add_to_cart | scroll_depth

    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(80), default="", index=True)
    query: Mapped[str] = mapped_column(String(255), default="")
    path: Mapped[str] = mapped_column(String(255), default="")
    dwell_ms: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")

    client_ts: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    user: Mapped[Optional["User"]] = relationship(back_populates="events")
    product: Mapped[Optional["Product"]] = relationship()

    @property
    def meta(self) -> dict[str, Any]:
        try:
            return json.loads(self.meta_json or "{}")
        except (ValueError, TypeError):
            return {}


class Recommendation(Base):
    """A stored agent output. The newest active row is what the user sees."""

    __tablename__ = "recommendations"
    __table_args__ = (Index("ix_reco_user_active", "user_id", "is_active", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    headline: Mapped[str] = mapped_column(String(255), default="")
    narrative: Mapped[str] = mapped_column(Text, default="")
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    interest_profile_json: Mapped[str] = mapped_column(Text, default="{}")

    # provenance / observability
    interest_signature: Mapped[str] = mapped_column(String(64), default="", index=True)
    trigger_reason: Mapped[str] = mapped_column(String(120), default="")
    model_used: Mapped[str] = mapped_column(String(120), default="")
    retrieval_queries: Mapped[str] = mapped_column(Text, default="[]")
    refine_loops: Mapped[int] = mapped_column(Integer, default=0)
    events_considered: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    user: Mapped["User"] = relationship(back_populates="recommendations")

    @property
    def items(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.items_json or "[]")
        except (ValueError, TypeError):
            return []

    @property
    def interest_profile(self) -> dict[str, Any]:
        try:
            return json.loads(self.interest_profile_json or "{}")
        except (ValueError, TypeError):
            return {}


class AgentState(Base):
    """Per-user bookkeeping that powers the trigger policy (the anti-waste layer)."""

    __tablename__ = "agent_state"
    __table_args__ = (UniqueConstraint("user_id", name="uq_agent_state_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_event_id_seen: Mapped[int] = mapped_column(Integer, default=0)
    last_signature: Mapped[str] = mapped_column(String(64), default="")
    runs_total: Mapped[int] = mapped_column(Integer, default=0)
    runs_skipped: Mapped[int] = mapped_column(Integer, default=0)
    last_digest_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="agent_state")
