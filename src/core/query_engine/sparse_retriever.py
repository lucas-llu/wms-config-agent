"""BM25 retrieval with vector-store hydration of text and metadata."""

from __future__ import annotations

from typing import Any

from core.types import RetrievalResult
from ingestion.storage import BM25Indexer
from libs.vector_store import BaseVectorStore


class SparseRetriever:
    def __init__(
        self,
        bm25_indexer: BM25Indexer,
        vector_store: BaseVectorStore,
        *,
        filter_candidate_multiplier: int = 5,
    ) -> None:
        if filter_candidate_multiplier <= 0:
            raise ValueError("filter_candidate_multiplier must be greater than 0")
        self.bm25_indexer = bm25_indexer
        self.vector_store = vector_store
        self.filter_candidate_multiplier = filter_candidate_multiplier

    def retrieve(
        self,
        keywords: list[str] | tuple[str, ...],
        top_k: int,
        filters: dict[str, Any] | None = None,
        trace: Any | None = None,
    ) -> list[RetrievalResult]:
        del trace
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0")
        candidate_count = top_k * self.filter_candidate_multiplier if filters else top_k
        ranked = self.bm25_indexer.query(list(keywords), top_k=candidate_count)
        if not ranked:
            return []

        records = self.vector_store.get_by_ids([str(result["chunk_id"]) for result in ranked])
        records_by_id = {str(record["id"]): record for record in records}
        results: list[RetrievalResult] = []
        for rank, hit in enumerate(ranked, start=1):
            chunk_id = str(hit["chunk_id"])
            record = records_by_id.get(chunk_id)
            if record is None:
                continue
            metadata = dict(record.get("metadata", {}))
            if filters and not self._matches(metadata, filters):
                continue
            score = float(hit["score"])
            results.append(
                RetrievalResult(
                    chunk_id=chunk_id,
                    score=score,
                    text=str(record.get("text", "")),
                    metadata=metadata,
                    retrieval_sources=("sparse",),
                    source_scores={"sparse": score},
                    source_ranks={"sparse": rank},
                )
            )
            if len(results) == top_k:
                break
        return results

    @staticmethod
    def _matches(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
        return all(metadata.get(key) == value for key, value in filters.items())
