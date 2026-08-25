"""Embedding provider contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseEmbedding(ABC):
    """Create compatible dense vectors for documents and queries."""

    def fit(self, texts: list[str], *, force: bool = False) -> bool:
        """Fit corpus-dependent providers; return whether the model changed."""
        del texts, force
        return False

    @abstractmethod
    def embed(self, texts: list[str], trace: Any | None = None) -> list[list[float]]:
        """Embed a batch of documents."""

    def embed_query(self, query: str, trace: Any | None = None) -> list[float]:
        if not query.strip():
            raise ValueError("query must be a non-empty string")
        return self.embed([query], trace=trace)[0]

    @property
    @abstractmethod
    def signature(self) -> str:
        """Return an identity that changes when stored vectors become incompatible."""
