"""Mesh API client — the single gateway for every LLM/AI call in this project.

Mesh is OpenAI-compatible, so we point the official OpenAI SDK at it. Nothing else
in the codebase is allowed to talk to a model provider directly.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Optional

from ..config import settings

logger = logging.getLogger(__name__)

_client_lock = threading.Lock()
_client: Any = None
_client_failed = False


class MeshUnavailable(RuntimeError):
    """Raised when Mesh cannot be reached or is not configured."""


def get_client() -> Any:
    """Lazily build the OpenAI SDK client pointed at Mesh."""
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed:
        raise MeshUnavailable("Mesh client previously failed to initialise")
    with _client_lock:
        if _client is not None:
            return _client
        if not settings.mesh_configured:
            _client_failed = True
            raise MeshUnavailable("MESH_API_KEY is not set — add it to your .env")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            _client_failed = True
            raise MeshUnavailable(f"openai SDK unavailable: {exc}") from exc
        _client = OpenAI(
            base_url=settings.mesh_base_url,
            api_key=settings.mesh_api_key,
            timeout=settings.mesh_timeout,
            max_retries=0,  # we own the retry loop so we can log/backoff
        )
        return _client


def chat(
    messages: list[dict[str, str]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 900,
    response_format: Optional[dict[str, str]] = None,
) -> str:
    """Single chat completion through Mesh. Returns raw assistant text."""
    client = get_client()
    model = model or settings.mesh_chat_model
    last_error: Optional[Exception] = None

    for attempt in range(settings.mesh_max_retries + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format:
                kwargs["response_format"] = response_format
            started = time.time()
            completion = client.chat.completions.create(**kwargs)
            logger.info(
                "mesh.chat model=%s attempt=%s latency_ms=%s",
                model,
                attempt,
                int((time.time() - started) * 1000),
            )
            return (completion.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - provider errors vary
            last_error = exc
            # response_format is not supported by every model behind Mesh; drop and retry.
            if response_format and "response_format" in str(exc).lower():
                response_format = None
                continue
            if attempt < settings.mesh_max_retries:
                time.sleep(1.5 * (attempt + 1))

    raise MeshUnavailable(f"Mesh chat failed after retries: {last_error}")


def chat_json(
    messages: list[dict[str, str]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.6,
    max_tokens: int = 1100,
) -> dict[str, Any]:
    """Chat completion that must return a JSON object. Tolerates fenced output."""
    raw = chat(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    return parse_json_object(raw)


def parse_json_object(raw: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from a model response."""
    if not raw:
        raise ValueError("empty model response")
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"no JSON object found in response: {raw[:200]}")
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def embed(texts: list[str], *, model: Optional[str] = None) -> list[list[float]]:
    """Embeddings through Mesh. Raises MeshUnavailable so callers can fall back."""
    if not texts:
        return []
    client = get_client()
    model = model or settings.mesh_embedding_model
    try:
        response = client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in response.data]
    except Exception as exc:  # noqa: BLE001
        raise MeshUnavailable(f"Mesh embeddings failed: {exc}") from exc


def health() -> dict[str, Any]:
    """Lightweight status used by /api/health and the admin page."""
    if not settings.mesh_configured:
        return {"configured": False, "reachable": False, "detail": "MESH_API_KEY missing"}
    try:
        chat(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=5,
            temperature=0,
        )
        return {"configured": True, "reachable": True, "detail": "ok"}
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "reachable": False, "detail": str(exc)[:200]}
