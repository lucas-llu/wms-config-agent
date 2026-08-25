"""Deterministic reciprocal-rank fusion."""

from __future__ import annotations

from collections import defaultdict

from core.types import RetrievalResult


class ReciprocalRankFusion:
    def __init__(self, k: int = 60) -> None:
        if k <= 0:
            raise ValueError("k must be greater than 0")
        self.k = k

    def fuse(
        self,
        rankings: dict[str, list[RetrievalResult]],
        *,
        top_k: int | None = None,
    ) -> list[RetrievalResult]:
        if top_k is not None and top_k <= 0:
            raise ValueError("top_k must be greater than 0")

        scores: dict[str, float] = defaultdict(float)
        exemplars: dict[str, RetrievalResult] = {}
        source_scores: dict[str, dict[str, float]] = defaultdict(dict)
        source_ranks: dict[str, dict[str, int]] = defaultdict(dict)
        for source, results in sorted(rankings.items()):
            for rank, result in enumerate(results, start=1):
                scores[result.chunk_id] += 1.0 / (self.k + rank)
                exemplars.setdefault(result.chunk_id, result)
                source_scores[result.chunk_id][source] = result.score
                source_ranks[result.chunk_id][source] = rank

        fused = [
            RetrievalResult(
                chunk_id=chunk_id,
                score=score,
                text=exemplars[chunk_id].text,
                metadata=dict(exemplars[chunk_id].metadata),
                retrieval_sources=tuple(sorted(source_scores[chunk_id])),
                source_scores=source_scores[chunk_id],
                source_ranks=source_ranks[chunk_id],
            )
            for chunk_id, score in scores.items()
        ]
        fused.sort(key=lambda result: (-result.score, result.chunk_id))
        return fused[:top_k] if top_k is not None else fused
