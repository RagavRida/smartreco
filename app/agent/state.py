"""Shared state object passed between agent nodes."""
from __future__ import annotations

from typing import Any, Optional, TypedDict


class AgentGraphState(TypedDict, total=False):
    # --- inputs ---
    user_id: int
    user_name: str
    profile: dict[str, Any]
    behavior_summary: str
    catalog_categories: list[str]
    limit: int

    # --- analyze node ---
    intent: str
    interest_headline: str
    queries: list[str]
    level_filter: Optional[str]
    analysis_source: str  # "llm" | "heuristic"

    # --- decide / retrieve / grade ---
    should_retrieve: bool
    skip_reason: str
    raw_hits: list[dict[str, Any]]
    ranked_hits: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    grade: dict[str, Any]
    refine_loops: int

    # --- generate ---
    headline: str
    narrative: str
    items: list[dict[str, Any]]
    model_used: str
    is_fallback: bool

    # --- diagnostics ---
    trace: list[dict[str, Any]]
    errors: list[str]


def new_state(**kwargs: Any) -> AgentGraphState:
    state: AgentGraphState = {
        "queries": [],
        "raw_hits": [],
        "ranked_hits": [],
        "candidates": [],
        "items": [],
        "refine_loops": 0,
        "trace": [],
        "errors": [],
        "is_fallback": False,
        "analysis_source": "heuristic",
        "model_used": "",
    }
    state.update(kwargs)  # type: ignore[typeddict-item]
    return state


def note(state: AgentGraphState, node: str, **detail: Any) -> None:
    state.setdefault("trace", []).append({"node": node, **detail})
