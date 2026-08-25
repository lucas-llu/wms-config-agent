from __future__ import annotations

from pathlib import Path

import pytest

from core.query_engine import (
    DenseRetriever,
    HybridSearch,
    QueryProcessor,
    ReciprocalRankFusion,
    SparseRetriever,
)
from core.settings import RetrievalSettings
from core.types import Chunk
from ingestion import IndexingPipeline
from ingestion.storage import BM25Indexer
from libs.embedding import LocalLSAEmbedding
from libs.vector_store import ChromaStore
from observability.evaluation import (
    BenchmarkDataset,
    RetrievalBenchmarkRunner,
)

pytestmark = pytest.mark.e2e


def _chunk(
    chunk_id: str,
    code: str,
    title: str,
    text: str,
    *,
    domain: str,
) -> Chunk:
    return Chunk(
        id=chunk_id,
        text=text,
        metadata={
            "source_path": f"sanitized/{chunk_id}.pdf",
            "source_relative_path": f"sanitized/{chunk_id}.pdf",
            "file_hash": chunk_id,
            "title": title,
            "collection": "test",
            "domain": domain,
            "process_code": code,
            "document_type": "configuration",
            "page_start": 1,
            "page_end": 1,
        },
        start_offset=0,
        end_offset=len(text),
    )


def test_ingest_to_committed_public_benchmark(tmp_path) -> None:
    chunks = [
        _chunk(
            "putaway",
            "SWL.I.11.04",
            "Sorted Putaway Configuration",
            "Configure sorted putaway policy and storage location rules.",
            domain="Inbound",
        ),
        _chunk(
            "appointment",
            "SWL.I.01.01",
            "Appointment Creation",
            "Configure inbound appointment capacity and dock schedules.",
            domain="Inbound",
        ),
        _chunk(
            "replenishment-tour",
            "SWL.O.07.03",
            "Replenishment Tour Configuration",
            "Configure outbound replenishment tour rules and execution sequencing.",
            domain="Outbound",
        ),
        _chunk(
            "inventory-move",
            "SWL.S.01.02",
            "Inventory Move Configuration",
            "Configure stock management inventory move rules and location validation.",
            domain="Stock Management",
        ),
    ]
    embedding = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "model")
    store = ChromaStore(persist_path=tmp_path / "chroma", collection_name="chunks")
    bm25 = BM25Indexer(tmp_path / "bm25")
    IndexingPipeline(
        embedding=embedding,
        vector_store=store,
        bm25_indexer=bm25,
        batch_size=2,
    ).index(chunks)
    settings = RetrievalSettings(
        sparse_backend="bm25",
        fusion_algorithm="rrf",
        top_k_dense=3,
        top_k_sparse=3,
        top_k_final=5,
        rrf_k=60,
        max_chunks_per_document=2,
        min_fused_score=0.02,
    )
    search = HybridSearch(
        settings,
        QueryProcessor(),
        DenseRetriever(embedding, store),
        SparseRetriever(bm25, store),
        ReciprocalRankFusion(settings.rrf_k),
    )
    dataset = BenchmarkDataset.load(Path("tests/fixtures/golden_test_set.json"))

    report = RetrievalBenchmarkRunner(search, top_k=5).run(dataset)

    assert report.passed is True
    assert report.case_count == 4
    assert report.metrics["hit_at_3"] == 1.0
    assert report.metrics["evidence_accuracy"] == 1.0
