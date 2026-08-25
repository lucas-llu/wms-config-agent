from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from observability.dashboard.services import EvaluationService
from observability.evaluation import BenchmarkCaseResult, BenchmarkReport


class _Runner:
    def __init__(self, report: BenchmarkReport) -> None:
        self.report = report

    def run(self, dataset) -> BenchmarkReport:
        assert dataset.fingerprint == self.report.dataset_fingerprint
        return self.report


def _dataset(path: Path) -> str:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "Sanitized fixture",
                "description": "Synthetic release benchmark",
                "thresholds": {"hit_at_3_min": 1.0},
                "test_cases": [
                    {
                        "id": "safe-case",
                        "category": "retrieval",
                        "query": "synthetic putaway rule",
                        "expected": {"text_contains": ["putaway"]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    from observability.evaluation import BenchmarkDataset

    return BenchmarkDataset.load(path).fingerprint


def _report(fingerprint: str, *, passed: bool = True) -> BenchmarkReport:
    case = BenchmarkCaseResult(
        case_id="safe-case",
        category="retrieval",
        query="synthetic putaway rule",
        expected_refusal=False,
        evidence_sufficient=passed,
        first_relevant_rank=1 if passed else None,
        elapsed_ms=1.0,
        passed=passed,
        top_results=(
            {
                "chunk_id": "safe-chunk",
                "score": 1.0,
                "source": "sanitized.pdf",
            },
        ),
        relevant_ranks={"dense": 1, "sparse": 1, "fused": 1, "final": 1},
        retrieval_counts={"dense": 1, "sparse": 1, "fused": 1, "final": 1},
        retrieval_failures={"dense": "secret provider diagnostic"},
    )
    score = 1.0 if passed else 0.0
    return BenchmarkReport(
        dataset_name="Sanitized fixture",
        dataset_fingerprint=fingerprint,
        created_at="2026-08-25T00:00:00+00:00",
        top_k=5,
        case_count=1,
        metrics={
            "case_count": 1,
            "hit_at_1": score,
            "hit_at_3": score,
            "hit_at_5": score,
            "mrr_at_5": score,
            "refusal_accuracy": None,
            "evidence_accuracy": score,
            "p95_latency_ms": 1.0,
        },
        category_metrics={"retrieval": {"hit_at_3": score}},
        thresholds={"hit_at_3_min": 1.0},
        threshold_results={"hit_at_3_min": passed},
        evaluation={
            "evaluator": "custom",
            "passed": passed,
            "details": {"unsafe": "secret evaluator diagnostic"},
        },
        passed=passed,
        cases=(case,),
    )


def test_evaluation_service_runs_approved_dataset_and_persists_safe_history(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "safe.json"
    fingerprint = _dataset(dataset_path)
    report = _report(fingerprint)
    service = EvaluationService(lambda: _Runner(report), [dataset_path], tmp_path / "reports")

    option = service.list_datasets()[0]
    result = service.run(option.identifier)
    history = service.list_reports(dataset_fingerprint=fingerprint)

    assert result.report.passed is True
    assert len(history) == 1
    assert history[0].metrics["hit_at_3"] == 1.0
    payload = (tmp_path / "reports" / history[0].identifier).read_text(encoding="utf-8")
    assert "synthetic putaway rule" not in payload
    assert "safe-chunk" not in payload
    assert "secret provider diagnostic" not in payload
    assert "secret evaluator diagnostic" not in payload
    assert "safe-case" in payload


def test_evaluation_service_compares_compatible_privacy_safe_history(tmp_path: Path) -> None:
    dataset_path = tmp_path / "safe.json"
    fingerprint = _dataset(dataset_path)
    current = _report(fingerprint)
    service = EvaluationService(lambda: _Runner(current), [dataset_path], tmp_path / "reports")
    first = service.run(fingerprint)

    compared = service.run(fingerprint, baseline_identifier=first.report_summary.identifier)

    assert compared.comparison is not None
    assert compared.comparison.passed is True
    assert compared.comparison.metric_deltas["hit_at_3"] == 0.0


def test_evaluation_service_rejects_unknown_ids_and_skips_malformed_history(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "safe.json"
    fingerprint = _dataset(dataset_path)
    service = EvaluationService(
        lambda: _Runner(_report(fingerprint)), [dataset_path], tmp_path / "reports"
    )
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "malformed.json").write_text("{broken", encoding="utf-8")

    assert service.list_reports() == ()
    with pytest.raises(ValueError, match="unapproved"):
        service.run("../private.json")
    with pytest.raises(ValueError, match="Unknown benchmark report"):
        service.run(fingerprint, baseline_identifier="../outside.json")


def test_evaluation_service_propagates_runner_failure_without_writing_report(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "safe.json"
    fingerprint = _dataset(dataset_path)

    def fail_runner():
        raise RuntimeError("embedding provider unavailable")

    service = EvaluationService(fail_runner, [dataset_path], tmp_path / "reports")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        service.run(fingerprint)
    assert not (tmp_path / "reports").exists()


def test_evaluation_history_identifies_failed_cases_without_content(tmp_path: Path) -> None:
    dataset_path = tmp_path / "safe.json"
    fingerprint = _dataset(dataset_path)
    failed = _report(fingerprint, passed=False)
    failed = replace(failed, cases=(replace(failed.cases[0], passed=False),))
    service = EvaluationService(lambda: _Runner(failed), [dataset_path], tmp_path / "reports")

    service.run(fingerprint)

    assert service.list_reports()[0].failed_cases == ("safe-case",)
