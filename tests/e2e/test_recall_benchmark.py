from __future__ import annotations

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
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkExpectation,
    RetrievalBenchmarkRunner,
)

pytestmark = pytest.mark.e2e


def _chunk(chunk_id: str, code: str, title: str, text: str) -> Chunk:
    return Chunk(
        id=chunk_id,
        text=text,
        metadata={
            "source_path": f"private/{chunk_id}.pdf",
            "source_relative_path": f"sanitized/{chunk_id}.pdf",
            "file_hash": chunk_id,
            "title": title,
            "collection": "test",
            "domain": "Inbound",
            "process_code": code,
            "document_type": "configuration",
            "page_start": 1,
            "page_end": 1,
        },
        start_offset=0,
        end_offset=len(text),
    )


def test_ingest_to_benchmark_recall_and_refusal(tmp_path) -> None:
    chunks = [
        _chunk(
            "putaway",
            "PROCESS-1",
            "RF Directed Putaway",
            "Configure directed putaway policy and storage location rules.",
        ),
        _chunk(
            "appointment",
            "PROCESS-2",
            "Appointment Creation",
            "Configure inbound appointment capacity and dock schedules.",
        ),
        _chunk(
            "cycle-count",
            "PROCESS-3",
            "RF Cycle Count",
            "Configure inventory cycle count plans and tolerances.",
        ),
    ]
    embedding = LocalLSAEmbedding(dimensions=2, cache_dir=tmp_path / "model")
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
    dataset = BenchmarkDataset(
        name="e2e",
        description="Sanitized end-to-end recall test",
        thresholds={
            "hit_at_3_min": 1.0,
            "refusal_accuracy_min": 1.0,
            "evidence_accuracy_min": 1.0,
        },
        test_cases=(
            BenchmarkCase(
                case_id="putaway",
                category="semantic",
                query="directed putaway storage location configuration",
                expected=BenchmarkExpectation(
                    chunk_ids=("putaway",),
                    process_codes=("PROCESS-1",),
                    text_contains=("storage location rules",),
                ),
            ),
            BenchmarkCase(
                case_id="appointment",
                category="semantic",
                query="inbound appointment dock configuration",
                expected=BenchmarkExpectation(
                    chunk_ids=("appointment",),
                    process_codes=("PROCESS-2",),
                    text_contains=("dock schedules",),
                ),
            ),
            BenchmarkCase(
                case_id="refusal",
                category="unsupported_refusal",
                query="quantum payroll satellite configuration",
                expected=BenchmarkExpectation(should_refuse=True),
            ),
        ),
    )

    report = RetrievalBenchmarkRunner(search, top_k=5).run(dataset)

    assert report.passed is True
    assert report.metrics["hit_at_3"] == 1.0
    assert report.metrics["refusal_accuracy"] == 1.0
