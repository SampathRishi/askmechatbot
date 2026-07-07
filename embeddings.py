"""
embeddings.py — pluggable embedding layer.

The rest of the system depends only on the `Embedder` interface, so an
API-based model (Anthropic/OpenAI/Voyage/etc.) can be dropped in later by
implementing the same three methods and registering it in `get_embedder()`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache
from typing import List

import config


class Embedder(ABC):
    """Minimal interface every embedding backend must satisfy."""

    name: str
    dimension: int

    @abstractmethod
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of documents (passages)."""

    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        """Embed a single search query."""


class SentenceTransformerEmbedder(Embedder):
    """Local, free sentence-transformers backend (default: BAAI/bge-small-en-v1.5).

    Embeddings are L2-normalized so cosine similarity == dot product and matches
    Chroma's cosine space. For bge-* models a short query instruction improves
    retrieval of section-specific passages; it is applied to queries only.
    """

    QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str = config.EMBEDDING_MODEL):
        from sentence_transformers import SentenceTransformer

        self.name = model_name
        self._model = SentenceTransformer(model_name)
        # method was renamed across sentence-transformers versions
        get_dim = getattr(self._model, "get_embedding_dimension", None) \
            or self._model.get_sentence_embedding_dimension
        self.dimension = get_dim()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        vecs = self._model.encode(
            texts,
            batch_size=config.EMBEDDING_BATCH_SIZE,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return vecs.tolist()

    def embed_query(self, text: str) -> List[float]:
        vec = self._model.encode(
            self.QUERY_INSTRUCTION + text,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return vec.tolist()


# --- future backend template ------------------------------------------------
# class AnthropicVoyageEmbedder(Embedder):
#     """Example API-based backend. Implement the three methods, then register
#     it in get_embedder() under a new EMBEDDING_PROVIDER value."""
#     ...


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """Factory: return the configured embedder (cached as a singleton)."""
    provider = config.EMBEDDING_PROVIDER.lower()
    if provider in ("sentence-transformers", "st", "local"):
        return SentenceTransformerEmbedder(config.EMBEDDING_MODEL)
    raise ValueError(f"Unknown EMBEDDING_PROVIDER: {config.EMBEDDING_PROVIDER!r}")
