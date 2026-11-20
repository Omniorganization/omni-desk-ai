"""Optional embedding similarity for planner memory retrieval.

Off by default (`memory_retrieval.embeddings_enabled`). When on, the SQLite
experience store blends cosine similarity into the BM25 ranking and caches one
vector per memory and model, encrypted like the memory text when encryption at
rest is on. Install the `vector` extra for the default embedder.
"""
from __future__ import annotations

import math
from typing import Any, Protocol, Sequence


class Embedder(Protocol):
    model_name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Local sentence-transformers model; texts never leave the process."""

    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("memory_retrieval.embeddings_enabled needs the 'vector' extra: pip install 'omnidesk-agent[vector]'") from exc
        self.model_name = model_name
        self._model: Any = SentenceTransformer(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in vector] for vector in vectors]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
