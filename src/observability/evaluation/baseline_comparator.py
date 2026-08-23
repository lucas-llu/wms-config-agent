"""Compare a candidate benchmark report against a frozen baseline."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from observability.evaluation.retrieval_benchmark import BenchmarkReport


class BaselineComparisonError(ValueError):
    """Raised when two benchmark reports cannot be compared reliably."""


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    dataset_fingerprint: str
    metric_deltas: dict[str, float]
    regressions: tuple[str, ...]
    improvements: tuple[str, ...]
    fixed_cases: tuple[str, ...]
    new_failures: tuple[str, ...]
    persistent_failures: tuple[str, ...]
    rank_changes: dict[str, int]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BaselineComparator:
    QUALITY_METRICS = (
        "hit_at_1",
        "hit_at_3",
        "hit_at_5",
        "mrr_at_5",
        "refusal_accuracy",
        "evidence_accuracy",
    )

    def __init__(self, *, quality_tolerance: float = 0.0) -> None:
        if quality_tolerance < 0:
            raise ValueError("quality_tolerance must be non-negative")
        self.quality_tolerance = quality_tolerance

    def compare(
        self,
        baseline: dict[str, Any],
        candidate: BenchmarkReport | dict[str, Any],
    ) -> BenchmarkComparison:
        candidate_payload = (
            candidate.to_dict() if isinstance(candidate, BenchmarkReport) else candidate
        )
        baseline_fingerprint = baseline.get("dataset_fingerprint")
        candidate_fingerprint = candidate_payload.get("dataset_fingerprint")
        if not isinstance(baseline_fingerprint, str) or (
            baseline_fingerprint != candidate_fingerprint
        ):
            raise BaselineComparisonError(
                "Baseline and candidate dataset fingerprints do not match"
            )
        baseline_metrics = _mapping(baseline.get("metrics"), "baseline.metrics")
        candidate_metrics = _mapping(
            candidate_payload.get("metrics"), "candidate.metrics"
        )
        deltas: dict[str, float] = {}
        regressions: list[str] = []
        improvements: list[str] = []
        for metric in self.QUALITY_METRICS:
            old = baseline_metrics.get(metric)
            new = candidate_metrics.get(metric)
            if old is None or new is None:
                continue
            delta = round(float(new) - float(old), 4)
            deltas[metric] = delta
            if delta < -self.quality_tolerance:
                regressions.append(metric)
            elif delta > self.quality_tolerance:
                improvements.append(metric)

        baseline_cases = _cases_by_id(baseline.get("cases"))
        candidate_cases = _cases_by_id(candidate_payload.get("cases"))
        if set(baseline_cases) != set(candidate_cases):
            raise BaselineComparisonError("Baseline and candidate case IDs do not match")
        fixed: list[str] = []
        new_failures: list[str] = []
        persistent: list[str] = []
        rank_changes: dict[str, int] = {}
        for case_id in sorted(baseline_cases):
            old = baseline_cases[case_id]
            new = candidate_cases[case_id]
            if not old.get("passed") and new.get("passed"):
                fixed.append(case_id)
            elif old.get("passed") and not new.get("passed"):
                new_failures.append(case_id)
            elif not old.get("passed") and not new.get("passed"):
                persistent.append(case_id)
            old_rank = old.get("first_relevant_rank")
            new_rank = new.get("first_relevant_rank")
            if isinstance(old_rank, int) and isinstance(new_rank, int) and old_rank != new_rank:
                rank_changes[case_id] = old_rank - new_rank
        return BenchmarkComparison(
            dataset_fingerprint=baseline_fingerprint,
            metric_deltas=deltas,
            regressions=tuple(regressions),
            improvements=tuple(improvements),
            fixed_cases=tuple(fixed),
            new_failures=tuple(new_failures),
            persistent_failures=tuple(persistent),
            rank_changes=rank_changes,
            passed=not regressions and not new_failures,
        )

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BaselineComparisonError(f"Baseline does not exist: {path}") from exc
        except json.JSONDecodeError as exc:
            raise BaselineComparisonError(f"Invalid baseline JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BaselineComparisonError("Baseline root must be an object")
        return payload


def _mapping(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BaselineComparisonError(f"{label} must be an object")
    return payload


def _cases_by_id(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, list | tuple):
        raise BaselineComparisonError("cases must be an array")
    cases: dict[str, dict[str, Any]] = {}
    for case in payload:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise BaselineComparisonError("Every report case requires a case_id")
        cases[case["case_id"]] = case
    if len(cases) != len(payload):
        raise BaselineComparisonError("Report case IDs must be unique")
    return cases
