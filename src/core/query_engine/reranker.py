"""Safe reranker orchestration with deterministic fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.types import RetrievalResult
from libs.reranker import BaseReranker


@dataclass(frozen=True, slots=True)
class RerankOutcome:
    results: tuple[RetrievalResult, ...]
    fallback_used: bool
    failure: str | None = None


class SafeReranker:
    def __init__(self, backend: BaseReranker) -> None:
        self.backend = backend

    def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        trace: Any | None = None,
    ) -> RerankOutcome:
        try:
            results = self.backend.rerank(query, candidates, trace=trace)
            if len(results) != len(candidates) or {
                result.chunk_id for result in results
            } != {result.chunk_id for result in candidates}:
                raise ValueError("reranker must return every candidate exactly once")
            return RerankOutcome(tuple(results), fallback_used=False)
        except Exception as exc:
            return RerankOutcome(
                tuple(candidates),
                fallback_used=True,
                failure=f"{type(exc).__name__}: {exc}",
            )
