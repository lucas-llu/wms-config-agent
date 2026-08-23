from __future__ import annotations

from typing import Any

from core.query_engine import HybridSearch, QueryProcessor, ReciprocalRankFusion
from core.settings import RetrievalSettings
from core.types import RetrievalResult


class BrokenDenseRetriever:
    def retrieve(self, *args: Any, **kwargs: Any) -> list[RetrievalResult]:
        del args, kwargs
        raise RuntimeError("dense unavailable")


class WorkingSparseRetriever:
    def retrieve(self, *args: Any, **kwargs: Any) -> list[RetrievalResult]:
        del args, kwargs
        return [
            RetrievalResult(
                chunk_id="putaway",
                score=8.0,
                text="Directed putaway selects a storage location.",
                metadata={"source_path": "putaway.pdf", "file_hash": "putaway"},
                retrieval_sources=("sparse",),
            )
        ]


def test_hybrid_search_degrades_to_working_retriever() -> None:
    settings = RetrievalSettings(
        sparse_backend="bm25",
        fusion_algorithm="rrf",
        top_k_dense=5,
        top_k_sparse=5,
        top_k_final=3,
        rrf_k=60,
        max_chunks_per_document=2,
        min_fused_score=0.02,
    )
    search = HybridSearch(
        settings,
        QueryProcessor(),
        BrokenDenseRetriever(),  # type: ignore[arg-type]
        WorkingSparseRetriever(),  # type: ignore[arg-type]
        ReciprocalRankFusion(settings.rrf_k),
    )

    outcome = search.search_with_details("directed putaway storage location")

    assert [result.chunk_id for result in outcome.results] == ["putaway"]
    assert outcome.failures == {"dense": "RuntimeError: dense unavailable"}
    assert outcome.evidence_sufficient is True
