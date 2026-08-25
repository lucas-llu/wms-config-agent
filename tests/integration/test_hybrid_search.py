from __future__ import annotations

import pytest

from core.query_engine import (
    DenseRetriever,
    HybridSearch,
    QueryProcessor,
    ReciprocalRankFusion,
    SafeReranker,
    SparseRetriever,
)
from core.settings import RetrievalSettings
from core.trace import TraceContext
from core.types import Chunk
from ingestion import IndexingPipeline
from ingestion.storage import BM25Indexer
from libs.embedding import LocalLSAEmbedding
from libs.reranker import NoneReranker
from libs.vector_store import ChromaStore

pytestmark = pytest.mark.integration


def _chunk(
    chunk_id: str,
    text: str,
    *,
    title: str,
    process_code: str,
    document_type: str,
) -> Chunk:
    return Chunk(
        id=chunk_id,
        text=text,
        metadata={
            "source_path": f"{chunk_id}.pdf",
            "source_relative_path": f"Inbound/{chunk_id}.pdf",
            "file_hash": chunk_id,
            "title": title,
            "process_code": process_code,
            "process_stage": "I3.Putaway",
            "domain": "Inbound",
            "document_type": document_type,
            "page_start": 1,
            "page_end": 1,
        },
        start_offset=0,
        end_offset=len(text),
    )


def test_hybrid_search_handles_chinese_expansion_filter_and_diversity(tmp_path) -> None:
    chunks = [
        _chunk(
            "putaway-config-1",
            "Putaway policy configuration selects a directed storage location.",
            title="RF Directed Putaway",
            process_code="SWL.I.11.01",
            document_type="configuration",
        ),
        _chunk(
            "putaway-config-2",
            "Configure location selection rules for directed putaway.",
            title="RF Directed Putaway",
            process_code="SWL.I.11.01",
            document_type="configuration",
        ),
        _chunk(
            "putaway-operation",
            "Scan the LPN and confirm the putaway location on RF.",
            title="RF Directed Putaway",
            process_code="SWL.I.11.01",
            document_type="operation",
        ),
        _chunk(
            "appointment-config",
            "Appointment configuration controls dock schedule capacity.",
            title="Appointment Creation",
            process_code="SWL.I.01.01",
            document_type="configuration",
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
        top_k_dense=4,
        top_k_sparse=4,
        top_k_final=3,
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

    trace = TraceContext("query")
    outcome = search.search_with_details("如何配置上架库位？", trace=trace)
    SafeReranker(NoneReranker()).rerank(
        outcome.processed_query.retrieval_query,
        list(outcome.results),
        trace=trace,
    )

    assert outcome.evidence_sufficient is True
    assert outcome.processed_query.filters["document_type"] == "configuration"
    assert outcome.results[0].metadata["process_code"] == "SWL.I.11.01"
    assert all(result.metadata["document_type"] == "configuration" for result in outcome.results)
    assert sum(result.metadata["process_code"] == "SWL.I.11.01" for result in outcome.results) <= 2

    stages = {stage["name"]: stage["details"] for stage in trace.to_dict()["stages"]}
    for stage_name in (
        "query_processing",
        "dense_retrieval",
        "sparse_retrieval",
        "fusion",
        "rerank",
    ):
        assert stages[stage_name]["method"]
        assert stages[stage_name]["provider"]
    assert stages["dense_retrieval"]["results"][0] == {
        "chunk_id": outcome.dense_results[0].chunk_id,
        "rank": 1,
        "score": round(outcome.dense_results[0].score, 8),
        "source_scores": {"dense": round(outcome.dense_results[0].source_scores["dense"], 8)},
        "source_ranks": {"dense": 1},
    }
    assert stages["fusion"]["rankings"]["dense"]
    assert stages["fusion"]["rankings"]["sparse"]
    assert stages["fusion"]["rankings"]["fused"]
    assert stages["rerank"]["before"] == stages["rerank"]["after"]
    assert "Directed putaway selects a storage location" not in str(trace.to_dict())

    unsupported = search.search_with_details("quantum inventory portal configuration")
    assert unsupported.results
    assert unsupported.evidence_sufficient is False
