"""Dense retrieval over a configured embedding provider and vector store."""

from __future__ import annotations

import math
from typing import Any

from core.types import RetrievalResult
from libs.embedding import BaseEmbedding
from libs.vector_store import BaseVectorStore


class DenseRetriever:
    def __init__(self, embedding: BaseEmbedding, vector_store: BaseVectorStore) -> None:
        self.embedding = embedding
        self.vector_store = vector_store

    def retrieve(
        self,
        query: str,
        top_k: int,
        filters: dict[str, Any] | None = None,
        trace: Any | None = None,
    ) -> list[RetrievalResult]:
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0")
        vector = self.embedding.embed_query(query, trace=trace)
        if not vector or not all(math.isfinite(value) for value in vector):
            raise ValueError("embedding provider returned an invalid query vector")
        if math.sqrt(sum(value * value for value in vector)) <= 1e-12:
            return []

        hits = self.vector_store.query(vector, top_k, filters=filters, trace=trace)
        return [
            RetrievalResult(
                chunk_id=str(hit["id"]),
                score=float(hit["score"]),
                text=str(hit.get("text", "")),
                metadata=dict(hit.get("metadata", {})),
                retrieval_sources=("dense",),
                source_scores={"dense": float(hit["score"])},
                source_ranks={"dense": rank},
            )
            for rank, hit in enumerate(hits, start=1)
        ]
