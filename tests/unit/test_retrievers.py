from __future__ import annotations

from typing import Any

from core.query_engine import DenseRetriever, SparseRetriever
from core.types import ChunkRecord
from libs.embedding import BaseEmbedding
from libs.vector_store import BaseVectorStore


class FakeEmbedding(BaseEmbedding):
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def embed(self, texts: list[str], trace: Any | None = None) -> list[list[float]]:
        del trace
        return [self.vector for _ in texts]

    @property
    def signature(self) -> str:
        return "fake"


class FakeStore(BaseVectorStore):
    def __init__(self) -> None:
        self.query_filters: dict[str, Any] | None = None
        self.records = {
            "a": {
                "id": "a",
                "text": "putaway configuration",
                "metadata": {
                    "source_path": "a.pdf",
                    "document_type": "configuration",
                },
            },
            "b": {
                "id": "b",
                "text": "putaway operation",
                "metadata": {
                    "source_path": "b.pdf",
                    "document_type": "operation",
                },
            },
        }

    def upsert(self, records: list[ChunkRecord], trace: Any | None = None) -> None:
        del records, trace

    def query(
        self,
        vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
        trace: Any | None = None,
    ) -> list[dict[str, Any]]:
        del vector, top_k, trace
        self.query_filters = filters
        return [{**self.records["a"], "score": 0.8}]

    def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        return [self.records[item] for item in ids if item in self.records]

    def count(self) -> int:
        return len(self.records)


class FakeBM25:
    def query(self, query: list[str], top_k: int) -> list[dict[str, Any]]:
        del query, top_k
        return [
            {"chunk_id": "b", "score": 4.0},
            {"chunk_id": "a", "score": 3.0},
        ]


def test_dense_retriever_normalizes_store_result() -> None:
    store = FakeStore()
    results = DenseRetriever(FakeEmbedding([1.0, 0.0]), store).retrieve(
        "putaway", 2, {"document_type": "configuration"}
    )

    assert results[0].chunk_id == "a"
    assert results[0].retrieval_sources == ("dense",)
    assert store.query_filters == {"document_type": "configuration"}


def test_dense_retriever_rejects_zero_vector_without_querying_store() -> None:
    store = FakeStore()

    assert DenseRetriever(FakeEmbedding([0.0, 0.0]), store).retrieve("中文", 2) == []
    assert store.query_filters is None


def test_sparse_retriever_hydrates_and_filters_results() -> None:
    results = SparseRetriever(FakeBM25(), FakeStore()).retrieve(
        ["putaway"], 2, {"document_type": "configuration"}
    )

    assert [result.chunk_id for result in results] == ["a"]
    assert results[0].source_scores == {"sparse": 3.0}
