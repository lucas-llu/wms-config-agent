"""No-op reranker used as the local fallback."""

from __future__ import annotations

from typing import Any

from core.types import RetrievalResult
from libs.reranker.base_reranker import BaseReranker


class NoneReranker(BaseReranker):
    def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        trace: Any | None = None,
    ) -> list[RetrievalResult]:
        del query, trace
        return list(candidates)
