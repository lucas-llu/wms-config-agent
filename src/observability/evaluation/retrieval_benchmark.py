"""Deterministic retrieval metrics over a validated benchmark dataset."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from core.query_engine import HybridSearch, SafeReranker
from core.types import RetrievalResult
from libs.evaluator import BaseEvaluator, EvaluationRequest, ThresholdEvaluator
from observability.evaluation.benchmark import BenchmarkCase, BenchmarkDataset


@dataclass(frozen=True, slots=True)
class BenchmarkCaseResult:
    case_id: str
    category: str
    query: str
    expected_refusal: bool
    evidence_sufficient: bool
    first_relevant_rank: int | None
    elapsed_ms: float
    passed: bool
    top_results: tuple[dict[str, Any], ...]
    relevant_ranks: dict[str, int | None]
    retrieval_counts: dict[str, int]
    retrieval_failures: dict[str, str]


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    dataset_name: str
    dataset_fingerprint: str
    created_at: str
    top_k: int
    case_count: int
    metrics: dict[str, float | int | None]
    category_metrics: dict[str, dict[str, float | int | None]]
    thresholds: dict[str, float]
    threshold_results: dict[str, bool]
    evaluation: dict[str, Any]
    passed: bool
    cases: tuple[BenchmarkCaseResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RetrievalBenchmarkRunner:
    def __init__(
        self,
        hybrid_search: HybridSearch,
        reranker: SafeReranker | None = None,
        *,
        top_k: int = 5,
        evaluator: BaseEvaluator | None = None,
    ) -> None:
        if top_k < 5:
            raise ValueError("Benchmark top_k must be at least 5")
        self.hybrid_search = hybrid_search
        self.reranker = reranker
        self.top_k = top_k
        self.evaluator = evaluator or ThresholdEvaluator()

    def run(self, dataset: BenchmarkDataset) -> BenchmarkReport:
        results = tuple(self._run_case(case) for case in dataset.test_cases)
        metrics = self._aggregate(results)
        categories = sorted({result.category for result in results})
        category_metrics = {
            category: self._aggregate(
                tuple(result for result in results if result.category == category)
            )
            for category in categories
        }
        evaluation = self.evaluator.evaluate(
            EvaluationRequest(
                metrics=metrics,
                thresholds=dataset.thresholds,
                context={
                    "dataset_name": dataset.name,
                    "dataset_fingerprint": dataset.fingerprint,
                },
            )
        )
        return BenchmarkReport(
            dataset_name=dataset.name,
            dataset_fingerprint=dataset.fingerprint,
            created_at=datetime.now(UTC).isoformat(),
            top_k=self.top_k,
            case_count=len(results),
            metrics=metrics,
            category_metrics=category_metrics,
            thresholds=dataset.thresholds,
            threshold_results=evaluation.checks,
            evaluation=evaluation.to_dict(),
            passed=evaluation.passed,
            cases=results,
        )

    def _run_case(self, case: BenchmarkCase) -> BenchmarkCaseResult:
        started = time.perf_counter()
        outcome = self.hybrid_search.search_with_details(
            case.query, top_k=self.top_k, filters=case.filters
        )
        if self.reranker is not None:
            reranked = self.reranker.rerank(
                outcome.processed_query.retrieval_query, list(outcome.results)
            )
            outcome = replace(outcome, results=reranked.results)
        elapsed_ms = (time.perf_counter() - started) * 1000
        relevant_ranks = {
            "dense": self._first_relevant_rank(case, outcome.dense_results),
            "sparse": self._first_relevant_rank(case, outcome.sparse_results),
            "fused": self._first_relevant_rank(case, outcome.fused_results),
            "final": self._first_relevant_rank(case, outcome.results),
        }
        rank = relevant_ranks["final"]
        passed = (
            not outcome.evidence_sufficient
            if case.expected.should_refuse
            else rank is not None and outcome.evidence_sufficient
        )
        return BenchmarkCaseResult(
            case_id=case.case_id,
            category=case.category,
            query=case.query,
            expected_refusal=case.expected.should_refuse,
            evidence_sufficient=outcome.evidence_sufficient,
            first_relevant_rank=rank,
            elapsed_ms=round(elapsed_ms, 3),
            passed=passed,
            top_results=tuple(self._safe_result(result) for result in outcome.results),
            relevant_ranks=relevant_ranks,
            retrieval_counts={
                "dense": len(outcome.dense_results),
                "sparse": len(outcome.sparse_results),
                "fused": len(outcome.fused_results),
                "final": len(outcome.results),
            },
            retrieval_failures=dict(outcome.failures),
        )

    @classmethod
    def _first_relevant_rank(
        cls,
        case: BenchmarkCase,
        results: tuple[RetrievalResult, ...],
    ) -> int | None:
        return next(
            (
                index
                for index, result in enumerate(results, start=1)
                if cls._is_relevant(case, result)
            ),
            None,
        )

    @staticmethod
    def _is_relevant(case: BenchmarkCase, result: RetrievalResult) -> bool:
        expected = case.expected
        metadata = result.metadata
        checks: list[bool] = []
        if expected.process_codes:
            checks.append(str(metadata.get("process_code", "")) in expected.process_codes)
        if expected.sources:
            source = str(
                metadata.get("source_relative_path")
                or metadata.get("source_name")
                or ""
            ).replace("\\", "/")
            checks.append(source in {value.replace("\\", "/") for value in expected.sources})
        if expected.domains:
            checks.append(str(metadata.get("domain", "")) in expected.domains)
        if expected.document_types:
            checks.append(
                str(metadata.get("document_type", "")) in expected.document_types
            )
        return bool(checks) and all(checks)

    @staticmethod
    def _safe_result(result: RetrievalResult) -> dict[str, Any]:
        return {
            "chunk_id": result.chunk_id,
            "score": round(result.score, 8),
            "process_code": result.metadata.get("process_code"),
            "source": result.metadata.get("source_relative_path")
            or result.metadata.get("source_name"),
            "domain": result.metadata.get("domain"),
            "document_type": result.metadata.get("document_type"),
            "page_start": result.metadata.get("page_start"),
            "page_end": result.metadata.get("page_end"),
        }

    @staticmethod
    def _aggregate(
        results: tuple[BenchmarkCaseResult, ...],
    ) -> dict[str, float | int | None]:
        positives = [result for result in results if not result.expected_refusal]
        negatives = [result for result in results if result.expected_refusal]
        elapsed = sorted(result.elapsed_ms for result in results)

        def hit_at(k: int) -> float:
            if not positives:
                return 0.0
            return sum(
                result.first_relevant_rank is not None
                and result.first_relevant_rank <= k
                for result in positives
            ) / len(positives)

        mrr = (
            sum(
                1 / result.first_relevant_rank
                for result in positives
                if result.first_relevant_rank is not None
                and result.first_relevant_rank <= 5
            )
            / len(positives)
            if positives
            else 0.0
        )
        refusal_accuracy = (
            sum(not result.evidence_sufficient for result in negatives) / len(negatives)
            if negatives
            else None
        )
        evidence_accuracy = (
            sum(result.passed for result in results) / len(results) if results else 0.0
        )
        return {
            "case_count": len(results),
            "positive_count": len(positives),
            "refusal_count": len(negatives),
            "hit_at_1": round(hit_at(1), 4),
            "hit_at_3": round(hit_at(3), 4),
            "hit_at_5": round(hit_at(5), 4),
            "mrr_at_5": round(mrr, 4),
            "refusal_accuracy": (
                round(refusal_accuracy, 4) if refusal_accuracy is not None else None
            ),
            "evidence_accuracy": round(evidence_accuracy, 4),
            "p50_latency_ms": round(_percentile(elapsed, 0.50), 3) if elapsed else None,
            "p95_latency_ms": round(_percentile(elapsed, 0.95), 3) if elapsed else None,
        }

def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    index = max(0, math.ceil(percentile * len(values)) - 1)
    return values[index]
