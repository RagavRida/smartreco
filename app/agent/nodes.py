"""The agent's reasoning nodes.

Each node is a plain function of (state) -> state so the graph can be executed by
LangGraph or, if LangGraph is unavailable, by the sequential fallback runner.
Every LLM call goes through Mesh; every node degrades to a deterministic path
rather than failing the whole run.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..config import settings
from ..services import behavior, retrieval
from ..services.mesh_client import MeshUnavailable, chat_json
from . import prompts
from .state import AgentGraphState, note

logger = logging.getLogger(__name__)

NodeFn = Callable[[AgentGraphState], AgentGraphState]


# --------------------------------------------------------------------------
# 1. ANALYZE — distil behaviour into a retrieval brief
# --------------------------------------------------------------------------
def make_analyze_node(db: Session) -> NodeFn:
    def analyze(state: AgentGraphState) -> AgentGraphState:
        profile = state.get("profile") or {}
        heuristic_queries = behavior.retrieval_queries(profile)

        if not profile.get("event_count"):
            state["intent"] = "New learner with no tracked activity yet."
            state["interest_headline"] = "Getting started"
            state["queries"] = heuristic_queries
            note(state, "analyze", source="empty-profile", queries=state["queries"])
            return state

        try:
            result = chat_json(
                [
                    {"role": "system", "content": prompts.ANALYZE_SYSTEM},
                    {
                        "role": "user",
                        "content": prompts.analyze_user_prompt(
                            state.get("behavior_summary", ""),
                            profile,
                            state.get("catalog_categories", []),
                        ),
                    },
                ],
                model=settings.mesh_chat_model,
                temperature=0.3,
                max_tokens=500,
            )
            queries = [q for q in (result.get("queries") or []) if isinstance(q, str) and q.strip()]
            state["intent"] = str(result.get("intent") or "").strip() or profile.get("summary", "")
            state["interest_headline"] = str(result.get("interest_headline") or "").strip()
            state["queries"] = (queries or heuristic_queries)[:4]
            level = result.get("level_filter")
            state["level_filter"] = level if level in {"beginner", "intermediate", "advanced"} else None
            state["analysis_source"] = "llm"
            note(state, "analyze", source="llm", queries=state["queries"], level=state.get("level_filter"))
        except (MeshUnavailable, ValueError) as exc:
            logger.warning("analyze node fell back to heuristics: %s", str(exc)[:160])
            state["intent"] = profile.get("summary", "")
            state["interest_headline"] = (
                profile.get("top_categories", [{}])[0].get("name", "Your interests")
                if profile.get("top_categories")
                else "Your interests"
            )
            state["queries"] = heuristic_queries
            state["level_filter"] = None
            state["analysis_source"] = "heuristic"
            state.setdefault("errors", []).append(f"analyze: {str(exc)[:160]}")
            note(state, "analyze", source="heuristic", queries=state["queries"])
        return state

    return analyze


# --------------------------------------------------------------------------
# 2. DECIDE — is retrieval warranted at all?
# --------------------------------------------------------------------------
def make_decide_node(db: Session) -> NodeFn:
    def decide(state: AgentGraphState) -> AgentGraphState:
        queries = state.get("queries") or []
        if not queries:
            state["should_retrieve"] = False
            state["skip_reason"] = "no usable retrieval queries"
        else:
            state["should_retrieve"] = True
            state["skip_reason"] = ""
        note(state, "decide", should_retrieve=state["should_retrieve"], reason=state.get("skip_reason"))
        return state

    return decide


# --------------------------------------------------------------------------
# 3. RETRIEVE — semantic search + re-rank + diversify (RAG)
# --------------------------------------------------------------------------
def make_retrieve_node(db: Session) -> NodeFn:
    def retrieve(state: AgentGraphState) -> AgentGraphState:
        queries = state.get("queries") or []
        profile = state.get("profile") or {}
        level = state.get("level_filter")
        hits = retrieval.multi_query_search(queries, k=settings.rec_retrieval_k, level=level)

        # A metadata filter should shape the ranking, not starve the candidate pool.
        # If filtering left us thin, top up with unfiltered matches at a slight penalty
        # so the filtered ones still win, but the generator has real choice.
        needed = int(state.get("limit") or settings.rec_products_returned) + 2
        if level and len(hits) < needed:
            seen = {h["id"] for h in hits}
            for extra in retrieval.multi_query_search(queries, k=settings.rec_retrieval_k):
                if extra["id"] in seen:
                    continue
                extra = dict(extra)
                extra["retrieval_score"] = extra.get("retrieval_score", extra["score"]) * 0.85
                hits.append(extra)
            note(state, "retrieve", topped_up_past_level_filter=level, pool=len(hits))

        ranked = retrieval.rerank(hits, profile)
        state["raw_hits"] = hits
        state["ranked_hits"] = ranked
        note(
            state,
            "retrieve",
            queries=queries,
            retrieved=len(hits),
            top_title=(ranked[0].get("metadata", {}).get("title") if ranked else None),
            top_score=(ranked[0].get("final_score") if ranked else 0.0),
        )
        return state

    return retrieve


# --------------------------------------------------------------------------
# 4. GRADE — is what we retrieved actually good enough to generate from?
# --------------------------------------------------------------------------
def make_grade_node(db: Session) -> NodeFn:
    def grade(state: AgentGraphState) -> AgentGraphState:
        signal = retrieval.quality_signal(state.get("ranked_hits") or [])
        state["grade"] = signal
        note(state, "grade", **signal)
        return state

    return grade


# --------------------------------------------------------------------------
# 5. REFINE — rewrite the queries and loop back into retrieval (bounded)
# --------------------------------------------------------------------------
def make_refine_node(db: Session) -> NodeFn:
    def refine(state: AgentGraphState) -> AgentGraphState:
        state["refine_loops"] = int(state.get("refine_loops", 0)) + 1
        previous = state.get("queries") or []
        sample_titles = [
            (h.get("metadata") or {}).get("title", "") for h in (state.get("ranked_hits") or [])[:6]
        ]
        try:
            result = chat_json(
                [
                    {"role": "system", "content": prompts.REFINE_SYSTEM},
                    {
                        "role": "user",
                        "content": prompts.refine_user_prompt(
                            state.get("behavior_summary", ""),
                            previous,
                            state.get("grade", {}),
                            sample_titles,
                        ),
                    },
                ],
                temperature=0.5,
                max_tokens=350,
            )
            queries = [q for q in (result.get("queries") or []) if isinstance(q, str) and q.strip()]
            if queries:
                state["queries"] = queries[:4]
            note(state, "refine", loop=state["refine_loops"], queries=state["queries"],
                 reasoning=result.get("reasoning"))
        except (MeshUnavailable, ValueError) as exc:
            # Deterministic broadening: strip to the dominant category + top terms.
            profile = state.get("profile") or {}
            cats = [c["name"] for c in profile.get("top_categories", [])[:2]]
            terms = [t["name"] for t in profile.get("top_terms", [])[:3]]
            broadened = [" ".join(cats + terms).strip() or "popular courses"]
            broadened += cats
            state["queries"] = [q for q in broadened if q][:3]
            state.setdefault("errors", []).append(f"refine: {str(exc)[:120]}")
            note(state, "refine", loop=state["refine_loops"], source="heuristic", queries=state["queries"])
        return state

    return refine


# --------------------------------------------------------------------------
# 6. GENERATE — grounded, persuasive copy
# --------------------------------------------------------------------------
def make_generate_node(db: Session) -> NodeFn:
    def generate(state: AgentGraphState) -> AgentGraphState:
        limit = int(state.get("limit") or settings.rec_products_returned)
        profile = state.get("profile") or {}

        # Shortlist: diversify the re-ranked hits, then hydrate from SQL (source of truth).
        # A focused learner should not be force-fed unrelated categories, so the
        # diversity cap loosens when one interest clearly dominates.
        top_cats = profile.get("top_categories", [])
        concentrated = bool(top_cats) and (
            len(top_cats) == 1 or top_cats[0]["weight"] >= 2 * top_cats[1]["weight"]
        )
        cap = max(3, limit - 1) if concentrated else 2
        ranked_hits = state.get("ranked_hits") or []
        shortlist = retrieval.diversify(ranked_hits, limit=limit + 2, max_per_category=cap)

        # Diversity decides the *ordering* the model sees first; it should not
        # amputate the pool. Backfill with the next best matches so the generator
        # has genuine choice among strong candidates.
        pool_target = min(len(ranked_hits), max(limit * 2 + 2, limit + 2))
        chosen_ids = {h["id"] for h in shortlist}
        for hit in ranked_hits:
            if len(shortlist) >= pool_target:
                break
            if hit["id"] not in chosen_ids:
                shortlist.append(hit)
                chosen_ids.add(hit["id"])

        candidates = retrieval.hydrate(db, shortlist)

        if not candidates:
            candidates = retrieval.popular_fallback(db, limit)
            state["is_fallback"] = True
            note(state, "generate", candidates="popular-fallback", count=len(candidates))

        if not candidates:
            state["headline"] = "Your catalog is empty"
            state["narrative"] = "No published courses are available to recommend yet."
            state["items"] = []
            note(state, "generate", result="empty-catalog")
            return state

        by_id = {c["id"]: c for c in candidates}

        try:
            result = chat_json(
                [
                    {"role": "system", "content": prompts.GENERATE_SYSTEM},
                    {
                        "role": "user",
                        "content": prompts.generate_user_prompt(
                            state.get("user_name", ""),
                            state.get("behavior_summary", ""),
                            profile,
                            state.get("intent", ""),
                            candidates,
                            limit,
                        ),
                    },
                ],
                model=settings.mesh_reasoning_model,
                temperature=0.75,
                max_tokens=900,
            )
            items = _bind_items(result.get("items"), by_id, limit)
            if not items:
                raise ValueError("model returned no valid catalog ids")
            state["headline"] = str(result.get("headline") or "").strip()[:200] or _default_headline(profile)
            state["narrative"] = str(result.get("narrative") or "").strip()
            state["items"] = items
            state["model_used"] = settings.mesh_reasoning_model
            note(state, "generate", source="llm", items=len(items))
        except (MeshUnavailable, ValueError) as exc:
            logger.warning("generate node fell back to template copy: %s", str(exc)[:160])
            state.update(_template_generation(profile, candidates, limit))  # type: ignore[arg-type]
            state["is_fallback"] = True
            state["model_used"] = "template-fallback"
            state.setdefault("errors", []).append(f"generate: {str(exc)[:160]}")
            note(state, "generate", source="template-fallback", items=len(state.get("items", [])))
        return state

    return generate


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _bind_items(raw: Any, by_id: dict[int, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Bind model output to real catalog rows. Anything hallucinated is dropped."""
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        try:
            pid = int(entry.get("product_id"))
        except (TypeError, ValueError):
            continue
        product = by_id.get(pid)
        if product is None or pid in seen:
            continue
        seen.add(pid)
        item = dict(product)
        item["reason"] = str(entry.get("reason") or "").strip()[:300]
        item["hook"] = str(entry.get("hook") or "").strip()[:120]
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _default_headline(profile: dict[str, Any]) -> str:
    cats = profile.get("top_categories") or []
    if cats:
        return f"Your next step in {cats[0]['name']}"
    return "Picked for you"


def _template_generation(
    profile: dict[str, Any], candidates: list[dict[str, Any]], limit: int
) -> dict[str, Any]:
    """Deterministic copy so the dashboard is never blank when Mesh is down."""
    cats = [c["name"] for c in profile.get("top_categories", [])[:2]]
    searches = profile.get("recent_searches", [])[:2]
    focus = cats[0] if cats else "your interests"
    detail = f" after searching for \"{searches[0]}\"" if searches else ""
    narrative = (
        f"You have been circling {focus}{detail}, spending "
        f"{profile.get('total_dwell_seconds', 0)}s across {profile.get('event_count', 0)} actions. "
        "These picks line up with that focus and build on where you already are."
    )
    items = []
    for product in candidates[:limit]:
        item = dict(product)
        item["reason"] = (
            f"Matches your interest in {product['category']} at {product['level']} level."
        )
        item["hook"] = f"{product['level'].title()} · {product['duration_hours']}h"
        items.append(item)
    return {
        "headline": _default_headline(profile),
        "narrative": narrative,
        "items": items,
    }
