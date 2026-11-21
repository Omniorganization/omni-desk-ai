"""Optional embedding similarity for planner memory retrieval.

Off by default (`memory_retrieval.embeddings_enabled`). When on, the SQLite
experience store blends cosine similarity into the BM25 ranking and caches one
vector per memory and model version, encrypted like the memory text when
encryption at rest is on. Install the `vector` extra for the default embedder.

The daemon never downloads a model: it loads one from local files only (a
directory, or a Hugging Face cache snapshot pinned by commit), so it makes no
network request for embeddings, whatever runtime.offline_mode says. Fetching
the model is a separate operator step; see docs/MEMORY_RETRIEVAL.md.
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Protocol, Sequence

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class Embedder(Protocol):
    model_name: str  # identifies the vector space: cached vectors are reused only under the same name

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """A sentence-transformers model loaded from local files; texts never leave the process."""

    def __init__(self, model_name: str, *, revision: str = ""):
        local_directory = Path(model_name).expanduser().is_dir()
        if not local_directory and not _COMMIT_RE.fullmatch(revision):
            raise ValueError(
                "memory_retrieval.embedding_revision must pin the commit (40 hex characters) of the Hugging Face model "
                f"{model_name!r}, or embedding_model must be a local model directory"
            )
        # Also tell the Hugging Face libraries not to use the network; they read this on first import.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("memory_retrieval.embeddings_enabled needs the 'vector' extra: pip install 'omnidesk-agent[vector]'") from exc
        try:
            self._model: Any = SentenceTransformer(model_name, revision=revision or None, local_files_only=True)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"embedding model {model_name!r} (revision {revision or 'local directory'}) is not available locally; "
                "the daemon does not download models, fetch it first (docs/MEMORY_RETRIEVAL.md)"
            ) from exc
        self.model_name = f"{model_name}@{revision}" if revision else model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in vector] for vector in vectors]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
