"""End-to-end smoke test. Runs without a Mesh API key.

    python tests/smoke_test.py

Uses a throwaway SQLite database and vector store so it never touches your dev data.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the app at a temporary database + vector store BEFORE importing it.
TMP = Path(tempfile.mkdtemp(prefix="smartreco-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{TMP / 'test.db'}"
os.environ["CHROMA_PATH"] = str(TMP / "chroma")
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["REC_MIN_SECONDS_BETWEEN_RUNS"] = "0"
os.environ["MESH_API_KEY"] = ""

from app.database import init_db, session_scope  # noqa: E402
from app.models import Event, Product, User  # noqa: E402
from app.security import hash_password, make_session, read_session  # noqa: E402
from app.services import behavior, product_service, recommender, retrieval, trigger  # noqa: E402
from app.agent import nodes as agent_nodes  # noqa: E402
from app.agent.graph import engine_name  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


SAMPLE = [
    {
        "title": "Building Agentic AI Systems with LangGraph",
        "description": "Stateful agent graphs, tool nodes, retrieval grading and self-correction loops.",
        "category": "ai-engineering", "level": "advanced", "tags": "langgraph,agents,orchestration",
        "price": 149.0, "duration_hours": 14, "rating": 4.8, "is_published": True,
    },
    {
        "title": "Retrieval-Augmented Generation in Production",
        "description": "Chunking, hybrid retrieval, re-ranking and evaluation for real RAG systems.",
        "category": "ai-engineering", "level": "intermediate", "tags": "rag,vector-search,reranking",
        "price": 129.0, "duration_hours": 11, "rating": 4.7, "is_published": True,
    },
    {
        "title": "Modern CSS Layout and Design Systems",
        "description": "Grid, flexbox, design tokens and accessible component systems.",
        "category": "frontend", "level": "beginner", "tags": "css,grid,design-systems",
        "price": 65.0, "duration_hours": 7, "rating": 4.5, "is_published": True,
    },
]


def main() -> int:
    print("\nSmartReco smoke test")
    print(f"  scratch dir: {TMP}")
    init_db()

    # ---------------------------------------------------------------- auth
    print("\n[1] Auth")
    with session_scope() as db:
        user = User(email="t@example.com", name="Tester", password_hash=hash_password("secret123"))
        db.add(user)
        db.commit()
        db.refresh(user)
        uid = user.id
    from app.security import verify_password

    check("bcrypt verifies the right password", verify_password("secret123", user.password_hash))
    check("bcrypt rejects the wrong password", not verify_password("nope", user.password_hash))
    check("signed session round-trips", read_session(make_session(uid)) == uid)
    check("tampered session is rejected", read_session("garbage.token") is None)

    # ---------------------------------------------------------------- dual write
    print("\n[2] Dual-write to SQL + vector store")
    with session_scope() as db:
        for spec in SAMPLE:
            product_service.create_product(db, dict(spec))
        status = product_service.sync_status(db)
        check("all products created", status["sql_products"] == 3, str(status))
        check("vector store mirrors SQL", status["in_sync"], str(status))

        target = db.query(Product).filter(Product.category == "frontend").one()
        product_service.update_product(db, target, {**SAMPLE[2], "is_published": False})
        status = product_service.sync_status(db)
        check("unpublishing removes the vector document", status["vector_documents"] == 2, str(status))

        product_service.update_product(db, target, {**SAMPLE[2], "is_published": True})
        check("republishing restores it", product_service.sync_status(db)["vector_documents"] == 3)

        product_service.delete_product(db, target)
        status = product_service.sync_status(db)
        check("delete removes from both stores",
              status["sql_products"] == 2 and status["vector_documents"] == 2, str(status))
        check("reconcile reports no drift", product_service.reconcile(db)["removed_orphans"] == 0)

    # ---------------------------------------------------------------- retrieval
    print("\n[3] Semantic retrieval")
    hits = retrieval.multi_query_search(["building ai agents with graphs"], k=3)
    check("vector search returns results", len(hits) > 0)
    check("most relevant course ranks first",
          bool(hits) and "LangGraph" in hits[0]["metadata"]["title"],
          str([h["metadata"]["title"] for h in hits]))

    # ---------------------------------------------------------------- events
    print("\n[4] Event ingestion + behaviour profile")
    with session_scope() as db:
        product = db.query(Product).filter(Product.title.like("Building Agentic%")).one()
        rows = [
            Event(user_id=uid, event_type="search", query="ai agents", session_id="s"),
            Event(user_id=uid, event_type="product_view", product_id=product.id,
                  category=product.category, dwell_ms=61000,
                  meta_json=json.dumps({"level": "advanced", "tags": product.tag_list})),
            Event(user_id=uid, event_type="click", product_id=product.id, category=product.category),
            Event(user_id=uid, event_type="search", query="langgraph workflow"),
            Event(user_id=uid, event_type="page_view", path="/catalog"),
        ]
        db.bulk_save_objects(rows)
        db.commit()

        profile = behavior.build_profile(db, uid)
        check("events recorded", profile["event_count"] == 5, str(profile["event_count"]))
        check("dominant category inferred",
              profile["top_categories"][0]["name"] == "ai-engineering", str(profile["top_categories"]))
        check("searches captured", len(profile["recent_searches"]) == 2, str(profile["recent_searches"]))
        check("dwell aggregated", profile["total_dwell_seconds"] == 61.0)
        check("signature is stable",
              profile["signature"] == behavior.build_profile(db, uid)["signature"])

    # ---------------------------------------------------------------- trigger
    print("\n[5] Trigger policy")
    with session_scope() as db:
        profile = behavior.build_profile(db, uid)
        decision = trigger.evaluate(db, uid, profile["signature"])
        check("cold start runs the agent", decision.should_run, decision.reason)

    # ---------------------------------------------------------------- agent
    print("\n[6] Agent run (LangGraph)")
    print(f"  engine: {engine_name()}")
    with session_scope() as db:
        user = db.get(User, uid)
        result = recommender.get_recommendation(db, user)
        reco = result.recommendation
        check("agent produced a recommendation", reco is not None)
        check("recommendation has items", bool(reco and reco.items))
        check("narrative written", bool(reco and reco.narrative))
        nodes_hit = [t["node"] for t in result.trace]
        check("graph executed all core nodes",
              {"analyze", "decide", "retrieve", "grade", "generate"}.issubset(set(nodes_hit)),
              str(nodes_hit))
        catalog_ids = {p.id for p in db.query(Product).all()}
        check("every recommended id exists in the catalog",
              all(i["id"] in catalog_ids for i in (reco.items if reco else [])))

        cached = recommender.get_recommendation(db, user)
        check("second request served from cache", not cached.ran_agent, cached.decision["reason"])

    # ---------------------------------------------------------------- grounding
    print("\n[7] Grounding guarantee (hallucinated ids are dropped)")
    original = agent_nodes.chat_json

    def fake_chat_json(messages, **kwargs):
        system = messages[0]["content"]
        if "retrieval brief" in system:
            return {"intent": "wants agents", "interest_headline": "AI agents",
                    "queries": ["langgraph agents"], "level_filter": None}
        if "recommendation block" in system:
            return {
                "headline": "Your agent stack, one layer deeper",
                "narrative": "You searched for AI agents twice and lingered on the LangGraph course.",
                "items": [
                    {"product_id": 999999, "reason": "hallucinated", "hook": "not real"},
                    {"product_id": 1, "reason": "You spent a full minute here.", "hook": "Graphs, not prompts"},
                ],
            }
        return {"queries": ["ai agents"]}

    agent_nodes.chat_json = fake_chat_json
    try:
        with session_scope() as db:
            user = db.get(User, uid)
            result = recommender.get_recommendation(db, user, force=True)
            ids = [i["id"] for i in result.recommendation.items]
            check("fabricated product id was dropped", 999999 not in ids, str(ids))
            check("real product id was kept", 1 in ids, str(ids))
            check("LLM copy used (not the template)", not result.recommendation.is_fallback)
    finally:
        agent_nodes.chat_json = original

    # ---------------------------------------------------------------- done
    print(f"\n{PASSED} passed, {FAILED} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
