"""Reranker extension contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from core.types import RetrievalResult


class BaseReranker(ABC):
    @abstractmethod
    def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        trace: Any | None = None,
    ) -> list[RetrievalResult]:
        """Return candidates in preferred order."""
