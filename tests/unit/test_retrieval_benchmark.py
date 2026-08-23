from __future__ import annotations

from core.query_engine import ProcessedQuery, SearchOutcome
from core.types import RetrievalResult
from observability.evaluation import (
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkExpectation,
    RetrievalBenchmarkRunner,
)


class FakeSearch:
    def search_with_details(self, query, top_k=None, filters=None):
        del top_k, filters
        irrelevant = _result("wrong", "PROCESS-X", "wrong.pdf")
        relevant = _result("right", "PROCESS-1", "Inbound/putaway.pdf")
        results = () if query == "unsupported" else (irrelevant, relevant)
        processed = ProcessedQuery(
            original_query=query,
            normalized_query=query,
            retrieval_query=query,
            keywords=(query,),
            filters={},
            expansions=(),
            specific_terms=(query,),
        )
        return SearchOutcome(
            processed_query=processed,
            dense_results=results,
            sparse_results=results,
            fused_results=results,
            results=results,
            failures={},
            evidence_sufficient=bool(results),
        )


def _result(chunk_id: str, code: str, source: str) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=chunk_id,
        score=0.03,
        text="private text must not appear in the report",
        metadata={
            "source_path": f"C:/private/{source}",
            "source_relative_path": source,
            "process_code": code,
            "domain": "Inbound",
            "document_type": "configuration",
            "page_start": 1,
            "page_end": 1,
        },
    )


def test_runner_calculates_ranking_refusal_and_threshold_metrics() -> None:
    dataset = BenchmarkDataset(
        name="test-v1",
        description="test",
        thresholds={
            "hit_at_1_min": 0.0,
            "hit_at_3_min": 1.0,
            "mrr_at_5_min": 0.5,
            "refusal_accuracy_min": 1.0,
            "evidence_accuracy_min": 1.0,
        },
        test_cases=(
            BenchmarkCase(
                case_id="positive",
                category="semantic",
                query="putaway",
                expected=BenchmarkExpectation(
                    process_codes=("PROCESS-1",),
                    sources=("Inbound/putaway.pdf",),
                    domains=("Inbound",),
                    document_types=("configuration",),
                ),
            ),
            BenchmarkCase(
                case_id="negative",
                category="refusal",
                query="unsupported",
                expected=BenchmarkExpectation(should_refuse=True),
            ),
        ),
    )

    report = RetrievalBenchmarkRunner(FakeSearch(), top_k=5).run(dataset)

    assert report.metrics["hit_at_1"] == 0.0
    assert report.metrics["hit_at_3"] == 1.0
    assert report.metrics["mrr_at_5"] == 0.5
    assert report.metrics["refusal_accuracy"] == 1.0
    assert report.passed is True
    assert report.cases[0].first_relevant_rank == 2
    assert "text" not in report.cases[0].top_results[0]
    assert report.cases[0].top_results[0]["source"] == "wrong.pdf"
