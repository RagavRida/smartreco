"""The agent as an explicit LangGraph workflow.

    analyze ──> decide ──┬── (no queries) ─────────────> generate ──> END
                         └── retrieve ──> grade ──┬── ok ──────────> generate ──> END
                                                  └── weak ──> refine ──> retrieve
                                                       (bounded by REC_MAX_REFINE_LOOPS)

If langgraph is not installed the same nodes and the same edge logic run through a
small sequential executor, so behaviour is identical either way.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..config import settings
from .nodes import (
    make_analyze_node,
    make_decide_node,
    make_generate_node,
    make_grade_node,
    make_refine_node,
    make_retrieve_node,
)
from .state import AgentGraphState

logger = logging.getLogger(__name__)

_LANGGRAPH_AVAILABLE: Optional[bool] = None


def langgraph_available() -> bool:
    global _LANGGRAPH_AVAILABLE
    if _LANGGRAPH_AVAILABLE is None:
        try:
            import langgraph.graph  # noqa: F401

            _LANGGRAPH_AVAILABLE = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("langgraph unavailable (%s) — using sequential executor", str(exc)[:120])
            _LANGGRAPH_AVAILABLE = False
    return bool(_LANGGRAPH_AVAILABLE)


# --------------------------------------------------------------------------
# conditional edges (shared by both executors)
# --------------------------------------------------------------------------
def _after_decide(state: AgentGraphState) -> str:
    return "retrieve" if state.get("should_retrieve") else "generate"


def _after_grade(state: AgentGraphState) -> str:
    grade = state.get("grade") or {}
    loops = int(state.get("refine_loops", 0))
    if grade.get("ok"):
        return "generate"
    if loops >= settings.rec_max_refine_loops:
        return "generate"  # good enough beats looping forever
    return "refine"


# --------------------------------------------------------------------------
# LangGraph build
# --------------------------------------------------------------------------
def build_graph(db: Session) -> Any:
    from langgraph.graph import END, StateGraph

    builder = StateGraph(AgentGraphState)
    builder.add_node("analyze", make_analyze_node(db))
    builder.add_node("decide", make_decide_node(db))
    builder.add_node("retrieve", make_retrieve_node(db))
    builder.add_node("grade", make_grade_node(db))
    builder.add_node("refine", make_refine_node(db))
    builder.add_node("generate", make_generate_node(db))

    builder.set_entry_point("analyze")
    builder.add_edge("analyze", "decide")
    builder.add_conditional_edges("decide", _after_decide, {"retrieve": "retrieve", "generate": "generate"})
    builder.add_edge("retrieve", "grade")
    builder.add_conditional_edges("grade", _after_grade, {"refine": "refine", "generate": "generate"})
    builder.add_edge("refine", "retrieve")
    builder.add_edge("generate", END)
    return builder.compile()


# --------------------------------------------------------------------------
# sequential fallback executor
# --------------------------------------------------------------------------
def _run_sequential(db: Session, state: AgentGraphState) -> AgentGraphState:
    nodes = {
        "analyze": make_analyze_node(db),
        "decide": make_decide_node(db),
        "retrieve": make_retrieve_node(db),
        "grade": make_grade_node(db),
        "refine": make_refine_node(db),
        "generate": make_generate_node(db),
    }
    state = nodes["analyze"](state)
    state = nodes["decide"](state)

    if _after_decide(state) == "retrieve":
        guard = 0
        while True:
            guard += 1
            state = nodes["retrieve"](state)
            state = nodes["grade"](state)
            if _after_grade(state) == "generate" or guard > settings.rec_max_refine_loops + 1:
                break
            state = nodes["refine"](state)

    return nodes["generate"](state)


def run_graph(db: Session, state: AgentGraphState) -> AgentGraphState:
    """Execute the workflow with whichever engine is available."""
    if langgraph_available():
        try:
            compiled = build_graph(db)
            result = compiled.invoke(
                state,
                config={"recursion_limit": 4 * (settings.rec_max_refine_loops + 2) + 6},
            )
            return dict(result)  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001
            logger.error("LangGraph execution failed (%s) — retrying sequentially", str(exc)[:200])
            state.setdefault("errors", []).append(f"langgraph: {str(exc)[:160]}")
    return _run_sequential(db, state)


def engine_name() -> str:
    return "langgraph" if langgraph_available() else "sequential-fallback"
