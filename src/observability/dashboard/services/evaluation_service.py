"""Privacy-safe benchmark execution and history for the Dashboard."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from libs.atomic_file import replace_file_atomically
from observability.evaluation import (
    BaselineComparator,
    BenchmarkComparison,
    BenchmarkDataset,
    BenchmarkReport,
    RetrievalBenchmarkRunner,
)


@dataclass(frozen=True, slots=True)
class EvaluationDatasetOption:
    identifier: str
    name: str
    description: str
    case_count: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class EvaluationReportSummary:
    identifier: str
    dataset_name: str
    dataset_fingerprint: str
    created_at: str
    case_count: int
    metrics: dict[str, float | int | None]
    threshold_results: dict[str, bool]
    passed: bool
    failed_cases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationRunResult:
    report: BenchmarkReport
    comparison: BenchmarkComparison | None
    report_summary: EvaluationReportSummary


class EvaluationService:
    """Run approved datasets and retain only comparison-safe report fields."""

    def __init__(
        self,
        runner_factory: Callable[[], RetrievalBenchmarkRunner],
        dataset_paths: Iterable[str | Path],
        report_root: str | Path,
        *,
        history_limit: int = 100,
    ) -> None:
        if history_limit <= 0:
            raise ValueError("history_limit must be greater than 0")
        self.runner_factory = runner_factory
        self.dataset_paths = tuple(Path(path).resolve() for path in dataset_paths)
        if not self.dataset_paths:
            raise ValueError("At least one approved benchmark dataset is required")
        self.report_root = Path(report_root).resolve()
        self.history_limit = history_limit

    def list_datasets(self) -> tuple[EvaluationDatasetOption, ...]:
        options: dict[str, EvaluationDatasetOption] = {}
        for path in self.dataset_paths:
            dataset = BenchmarkDataset.load(path)
            options.setdefault(
                dataset.fingerprint,
                EvaluationDatasetOption(
                    identifier=dataset.fingerprint,
                    name=dataset.name,
                    description=dataset.description,
                    case_count=len(dataset.test_cases),
                    fingerprint=dataset.fingerprint,
                ),
            )
        return tuple(sorted(options.values(), key=lambda item: item.name.casefold()))

    def list_reports(
        self,
        *,
        dataset_fingerprint: str | None = None,
    ) -> tuple[EvaluationReportSummary, ...]:
        if not self.report_root.is_dir():
            return ()
        summaries: list[EvaluationReportSummary] = []
        paths = sorted(self.report_root.glob("*.json"), key=lambda path: path.name, reverse=True)
        for path in paths:
            try:
                summary = self._summary_from_payload(path.name, self._load_report_path(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if dataset_fingerprint and summary.dataset_fingerprint != dataset_fingerprint:
                continue
            summaries.append(summary)
            if len(summaries) >= self.history_limit:
                break
        return tuple(summaries)

    def run(
        self,
        dataset_identifier: str,
        *,
        baseline_identifier: str | None = None,
    ) -> EvaluationRunResult:
        dataset = self._load_approved_dataset(dataset_identifier)
        report = self.runner_factory().run(dataset)
        comparison = None
        if baseline_identifier:
            comparison = BaselineComparator().compare(
                self._load_report(baseline_identifier), report
            )
        payload = self._privacy_safe_payload(report, comparison)
        report_path = self._new_report_path(report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        replace_file_atomically(temporary, report_path)
        return EvaluationRunResult(
            report=report,
            comparison=comparison,
            report_summary=self._summary_from_payload(report_path.name, payload),
        )

    def _load_approved_dataset(self, identifier: str) -> BenchmarkDataset:
        for path in self.dataset_paths:
            dataset = BenchmarkDataset.load(path)
            if dataset.fingerprint == identifier:
                return dataset
        raise ValueError("Unknown or unapproved benchmark dataset")

    def _load_report(self, identifier: str) -> dict[str, Any]:
        if Path(identifier).name != identifier or not identifier.endswith(".json"):
            raise ValueError("Unknown benchmark report")
        known = {summary.identifier for summary in self.list_reports()}
        if identifier not in known:
            raise ValueError("Unknown benchmark report")
        return self._load_report_path(self.report_root / identifier)

    def _load_report_path(self, path: Path) -> dict[str, Any]:
        resolved = path.resolve()
        if resolved.parent != self.report_root:
            raise ValueError("Benchmark report must remain inside the report directory")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Benchmark report root must be an object")
        return payload

    @staticmethod
    def _privacy_safe_payload(
        report: BenchmarkReport,
        comparison: BenchmarkComparison | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "dataset_name": report.dataset_name,
            "dataset_fingerprint": report.dataset_fingerprint,
            "created_at": report.created_at,
            "top_k": report.top_k,
            "case_count": report.case_count,
            "metrics": report.metrics,
            "category_metrics": report.category_metrics,
            "thresholds": report.thresholds,
            "threshold_results": report.threshold_results,
            "passed": report.passed,
            "cases": [
                {
                    "case_id": case.case_id,
                    "category": case.category,
                    "expected_refusal": case.expected_refusal,
                    "evidence_sufficient": case.evidence_sufficient,
                    "first_relevant_rank": case.first_relevant_rank,
                    "passed": case.passed,
                    "relevant_ranks": case.relevant_ranks,
                    "retrieval_counts": case.retrieval_counts,
                }
                for case in report.cases
            ],
        }
        if comparison is not None:
            payload["comparison"] = comparison.to_dict()
        return payload

    @staticmethod
    def _summary_from_payload(
        identifier: str,
        payload: dict[str, Any],
    ) -> EvaluationReportSummary:
        dataset_name = payload.get("dataset_name")
        fingerprint = payload.get("dataset_fingerprint")
        created_at = payload.get("created_at")
        case_count = payload.get("case_count")
        metrics = payload.get("metrics")
        threshold_results = payload.get("threshold_results")
        passed = payload.get("passed")
        cases = payload.get("cases")
        if (
            payload.get("schema_version") != 1
            or not isinstance(dataset_name, str)
            or not isinstance(fingerprint, str)
            or not isinstance(created_at, str)
            or not isinstance(case_count, int)
            or not isinstance(metrics, dict)
            or not isinstance(threshold_results, dict)
            or not isinstance(passed, bool)
            or not isinstance(cases, list)
        ):
            raise ValueError("Unsupported benchmark history report")
        safe_metrics: dict[str, float | int | None] = {}
        for key, value in metrics.items():
            if isinstance(key, str) and (
                value is None or isinstance(value, int | float) and not isinstance(value, bool)
            ):
                safe_metrics[key] = value
        safe_thresholds = {
            key: value
            for key, value in threshold_results.items()
            if isinstance(key, str) and isinstance(value, bool)
        }
        failed_cases = tuple(
            str(case["case_id"])
            for case in cases
            if isinstance(case, dict)
            and isinstance(case.get("case_id"), str)
            and case.get("passed") is False
        )
        return EvaluationReportSummary(
            identifier=identifier,
            dataset_name=dataset_name,
            dataset_fingerprint=fingerprint,
            created_at=created_at,
            case_count=case_count,
            metrics=safe_metrics,
            threshold_results=safe_thresholds,
            passed=passed,
            failed_cases=failed_cases,
        )

    def _new_report_path(self, report: BenchmarkReport) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return self.report_root / f"{timestamp}-{report.dataset_fingerprint[:12]}.json"
