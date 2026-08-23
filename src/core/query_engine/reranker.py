"""Safe reranker orchestration with deterministic fallback."""

from __future__ import annotations

import time
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
        started = time.perf_counter()
        try:
            results = self.backend.rerank(query, candidates, trace=trace)
            if len(results) != len(candidates) or {
                result.chunk_id for result in results
            } != {result.chunk_id for result in candidates}:
                raise ValueError("reranker must return every candidate exactly once")
            outcome = RerankOutcome(tuple(results), fallback_used=False)
        except Exception as exc:
            outcome = RerankOutcome(
                tuple(candidates),
                fallback_used=True,
                failure=f"{type(exc).__name__}: {exc}",
            )
        if trace is not None and hasattr(trace, "record_stage"):
            trace.record_stage(
                "rerank",
                (time.perf_counter() - started) * 1000,
                details={
                    "candidate_count": len(candidates),
                    "fallback_used": outcome.fallback_used,
                },
            )
        return outcome
