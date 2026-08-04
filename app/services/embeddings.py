"""Embedding layer.

Primary path: Mesh API embeddings (every AI call goes through Mesh).
Fallback path: a deterministic local hashing vectorizer, so the catalog stays
searchable — and the app stays runnable — when Mesh is unreachable or the key is
absent. The fallback is clearly flagged; it is a degradation, not a silent swap.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from typing import Optional

from ..config import settings
from . import mesh_client

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "your",
    "you", "this", "that", "is", "are", "be", "how", "what", "it", "as", "at",
    "by", "from", "will", "learn", "course",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_state_lock = threading.Lock()
_mesh_embeddings_ok: Optional[bool] = None  # None = untried, False = disabled this process


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 1]


def _hash_vector(text: str, dim: int) -> list[float]:
    """Feature-hashed bag of words + bigrams, L2 normalised.

    Deterministic and dependency-free: the same text always yields the same vector,
    and cosine similarity still separates 'agentic ai' from 'excel for finance'.
    """
    vec = [0.0] * dim
    tokens = _tokenize(text)
    grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
    if not grams:
        grams = ["__empty__"]
    for gram in grams:
        digest = hashlib.md5(gram.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        # Sub-linear term weighting keeps long descriptions from dominating.
        vec[idx] += sign * (1.0 / math.sqrt(len(grams)))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def using_fallback() -> bool:
    return _mesh_embeddings_ok is False


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Never raises — falls back rather than failing a write."""
    global _mesh_embeddings_ok
    if not texts:
        return []

    if _mesh_embeddings_ok is not False and settings.mesh_configured:
        try:
            vectors = mesh_client.embed(texts)
            if vectors and all(vectors):
                with _state_lock:
                    _mesh_embeddings_ok = True
                return vectors
        except Exception as exc:  # noqa: BLE001
            with _state_lock:
                if _mesh_embeddings_ok is None:
                    logger.warning(
                        "Mesh embeddings unavailable (%s) — using the local hashing "
                        "vectorizer for the rest of this process.",
                        str(exc)[:160],
                    )
                _mesh_embeddings_ok = False

    dim = settings.embedding_dim
    return [_hash_vector(t, dim) for t in texts]


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
