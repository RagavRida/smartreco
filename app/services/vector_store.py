"""Vector database layer (ChromaDB), with a pure-Python fallback store.

We always supply embeddings ourselves (from services.embeddings, which routes to
Mesh) so Chroma never downloads its own embedding model.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from .embeddings import cosine, embed_text, embed_texts

logger = logging.getLogger(__name__)


class BaseVectorStore:
    backend = "base"

    def upsert(self, doc_id: str, document: str, metadata: dict[str, Any]) -> None:
        raise NotImplementedError

    def delete(self, doc_id: str) -> None:
        raise NotImplementedError

    def query(
        self, text: str, k: int = 10, where: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def all_ids(self) -> set[str]:
        raise NotImplementedError


class ChromaVectorStore(BaseVectorStore):
    backend = "chromadb"

    def __init__(self) -> None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=settings.chroma_path,
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection = self._client.get_or_create_collection(
            name=settings.chroma_collection,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, doc_id: str, document: str, metadata: dict[str, Any]) -> None:
        self._collection.upsert(
            ids=[doc_id],
            documents=[document],
            embeddings=[embed_text(document)],
            metadatas=[metadata],
        )

    def delete(self, doc_id: str) -> None:
        self._collection.delete(ids=[doc_id])

    def query(
        self, text: str, k: int = 10, where: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        if self.count() == 0:
            return []
        result = self._collection.query(
            query_embeddings=[embed_text(text)],
            n_results=min(k, max(self.count(), 1)),
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )
        hits: list[dict[str, Any]] = []
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        for i, doc_id in enumerate(ids):
            distance = dists[i] if i < len(dists) else 1.0
            hits.append(
                {
                    "id": doc_id,
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": dict(metas[i]) if i < len(metas) and metas[i] else {},
                    "score": max(0.0, 1.0 - float(distance)),  # cosine distance -> similarity
                }
            )
        return hits

    def count(self) -> int:
        try:
            return int(self._collection.count())
        except Exception:  # noqa: BLE001  pragma: no cover
            return 0

    def all_ids(self) -> set[str]:
        try:
            return set(self._collection.get(include=[]).get("ids") or [])
        except Exception:  # noqa: BLE001  pragma: no cover
            return set()


class JsonVectorStore(BaseVectorStore):
    """Dependency-free fallback: brute-force cosine over a JSON-backed index.

    Fine for a catalog of this size; keeps the whole system runnable if chromadb
    cannot be installed in the evaluation environment.
    """

    backend = "json-fallback"

    def __init__(self) -> None:
        self._path = Path(settings.chroma_path) / "fallback_index.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except ValueError:
                self._data = {}

    def _flush(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        tmp.replace(self._path)

    def upsert(self, doc_id: str, document: str, metadata: dict[str, Any]) -> None:
        with self._lock:
            self._data[doc_id] = {
                "document": document,
                "metadata": metadata,
                "embedding": embed_text(document),
            }
            self._flush()

    def delete(self, doc_id: str) -> None:
        with self._lock:
            self._data.pop(doc_id, None)
            self._flush()

    def query(
        self, text: str, k: int = 10, where: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        if not self._data:
            return []
        qvec = embed_text(text)
        hits = []
        for doc_id, row in self._data.items():
            meta = row.get("metadata") or {}
            if where and not _matches_where(meta, where):
                continue
            hits.append(
                {
                    "id": doc_id,
                    "document": row.get("document", ""),
                    "metadata": meta,
                    "score": cosine(qvec, row.get("embedding") or []),
                }
            )
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:k]

    def count(self) -> int:
        return len(self._data)

    def all_ids(self) -> set[str]:
        return set(self._data.keys())


def _matches_where(meta: dict[str, Any], where: dict[str, Any]) -> bool:
    """Supports the small subset of Chroma's where-syntax we actually use."""
    for key, condition in where.items():
        if key == "$and":
            return all(_matches_where(meta, c) for c in condition)
        if key == "$or":
            return any(_matches_where(meta, c) for c in condition)
        actual = meta.get(key)
        if isinstance(condition, dict):
            for op, expected in condition.items():
                if op == "$eq" and actual != expected:
                    return False
                if op == "$ne" and actual == expected:
                    return False
                if op == "$in" and actual not in expected:
                    return False
                if op == "$lte" and not (actual is not None and actual <= expected):
                    return False
                if op == "$gte" and not (actual is not None and actual >= expected):
                    return False
        elif actual != condition:
            return False
    return True


_store: Optional[BaseVectorStore] = None
_store_lock = threading.Lock()


def get_store() -> BaseVectorStore:
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        try:
            _store = ChromaVectorStore()
            logger.info("Vector store: chromadb at %s", settings.chroma_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("chromadb unavailable (%s) — using JSON fallback store", str(exc)[:160])
            _store = JsonVectorStore()
        return _store


def product_doc_id(product_id: int) -> str:
    return f"product:{product_id}"
