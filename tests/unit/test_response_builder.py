from __future__ import annotations

from core.query_engine import ProcessedQuery, SearchOutcome
from core.response import ResponseBuilder
from core.types import RetrievalResult


def _outcome(*, sufficient: bool) -> SearchOutcome:
    result = RetrievalResult(
        chunk_id="chunk-1",
        score=0.03,
        text="Configure the putaway policy.\nThen verify the directed location.",
        metadata={
            "source_path": "private/putaway.pdf",
            "source_relative_path": "Inbound/putaway.pdf",
            "title": "RF Directed Putaway",
            "process_code": "SWL.I.11.01",
            "document_type": "configuration",
            "page_start": 4,
            "page_end": 5,
        },
        retrieval_sources=("dense", "sparse"),
    )
    processed = ProcessedQuery(
        original_query="如何配置上架？",
        normalized_query="如何配置上架？",
        retrieval_query="如何配置上架？ putaway configuration",
        keywords=("putaway", "configuration"),
        filters={"document_type": "configuration"},
        expansions=("putaway", "configuration"),
        specific_terms=("putaway",),
    )
    results = (result,) if sufficient else ()
    return SearchOutcome(
        processed_query=processed,
        dense_results=results,
        sparse_results=results,
        fused_results=results,
        results=results,
        failures={},
        evidence_sufficient=sufficient,
    )


def test_response_builder_returns_cited_source_excerpt() -> None:
    response = ResponseBuilder().build(_outcome(sufficient=True))

    assert response.status == "evidence_found"
    assert "[1] RF Directed Putaway (SWL.I.11.01)" in response.markdown
    assert "第 4-5 页" in response.markdown
    assert response.citations[0].source == "Inbound/putaway.pdf"


def test_response_builder_refuses_without_sufficient_evidence() -> None:
    response = ResponseBuilder().build(_outcome(sufficient=False))

    assert response.status == "insufficient_evidence"
    assert response.citations == ()
    assert "未找到足够可靠" in response.markdown
