"""LangSmith tracing (bonus). No-ops cleanly when unconfigured."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from ..config import settings

logger = logging.getLogger(__name__)

_enabled = False


def setup() -> bool:
    """Wire LangSmith env vars so LangGraph/LangChain auto-trace the agent run."""
    global _enabled
    if not settings.langsmith_api_key or not settings.langsmith_tracing:
        logger.info("LangSmith tracing disabled (set LANGCHAIN_TRACING_V2=true + LANGCHAIN_API_KEY)")
        return False
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    _enabled = True
    logger.info("LangSmith tracing enabled — project=%s", settings.langsmith_project)
    return True


def is_enabled() -> bool:
    return _enabled


@contextmanager
def trace_run(name: str, **metadata: Any) -> Iterator[None]:
    """Wrap an agent run in a LangSmith trace when available; otherwise just log."""
    if not _enabled:
        logger.info("agent.run start name=%s meta=%s", name, metadata)
        yield
        logger.info("agent.run end name=%s", name)
        return
    try:
        from langsmith import tracing_context

        with tracing_context(enabled=True, tags=["smartreco", name], metadata=metadata):
            yield
        return
    except Exception as exc:  # noqa: BLE001 - never let observability break the run
        logger.debug("LangSmith context unavailable (%s) — continuing untraced", exc)
        yield
