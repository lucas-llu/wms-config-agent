"""Deterministic Agent golden-scenario evaluation contracts and metrics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_REQUIRED_CATEGORIES = {
    "normal_solution",
    "requirement_change",
    "evidence_gap",
    "version_conflict",
    "interrupt_recovery",
    "session_isolation",
}


@dataclass(frozen=True, slots=True)
class AgentGoldenScenario:
    scenario_id: str
    category: str
    description: str


@dataclass(frozen=True, slots=True)
class AgentGoldenDataset:
    name: str
    scenarios: tuple[AgentGoldenScenario, ...]
    thresholds: dict[str, float]

    @classmethod
    def load(cls, path: str | Path) -> AgentGoldenDataset:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("agent golden dataset schema_version must be 1")
        scenarios = tuple(AgentGoldenScenario(**item) for item in payload["scenarios"])
        categories = {item.category for item in scenarios}
        if categories != _REQUIRED_CATEGORIES:
            raise ValueError("agent golden dataset must contain the six release categories")
        if len({item.scenario_id for item in scenarios}) != len(scenarios):
            raise ValueError("agent golden scenario IDs must be unique")
        thresholds = {str(key): float(value) for key, value in payload["thresholds"].items()}
        return cls(str(payload["name"]), scenarios, thresholds)

    @property
    def fingerprint(self) -> str:
        value = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentScenarioResult:
    scenario_id: str
    intent_correct: bool = True
    required_fields_complete: bool = True
    duplicate_question_free: bool = True
    dag_valid: bool = True
    task_coverage: float = 1.0
    citation_coverage: float = 1.0
    citation_support: float = 1.0
    conflict_detected: bool = True
    evidence_gap_blocked: bool = True
    solution_complete: float = 1.0
    recovery_success: bool = True
    session_isolated: bool = True
    unauthorized_tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class AgentEvaluationReport:
    dataset_name: str
    dataset_fingerprint: str
    scenario_count: int
    metrics: dict[str, float]
    threshold_results: dict[str, bool]
    failed_scenarios: tuple[str, ...]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentEvaluationRunner:
    def run(
        self,
        dataset: AgentGoldenDataset,
        results: list[AgentScenarioResult],
    ) -> AgentEvaluationReport:
        by_id = {item.scenario_id: item for item in results}
        expected = {item.scenario_id for item in dataset.scenarios}
        if set(by_id) != expected:
            raise ValueError("agent results must match every golden scenario exactly once")
        ordered = [by_id[item.scenario_id] for item in dataset.scenarios]
        metrics = {
            "intent_accuracy": _mean(item.intent_correct for item in ordered),
            "required_field_completion": _mean(item.required_fields_complete for item in ordered),
            "duplicate_question_avoidance": _mean(item.duplicate_question_free for item in ordered),
            "dag_validity": _mean(item.dag_valid for item in ordered),
            "task_coverage": _average(item.task_coverage for item in ordered),
            "citation_coverage": _average(item.citation_coverage for item in ordered),
            "citation_support": _average(item.citation_support for item in ordered),
            "conflict_detection": _mean(item.conflict_detected for item in ordered),
            "evidence_gap_blocking": _mean(item.evidence_gap_blocked for item in ordered),
            "solution_completeness": _average(item.solution_complete for item in ordered),
            "recovery_success": _mean(item.recovery_success for item in ordered),
            "session_isolation": _mean(item.session_isolated for item in ordered),
            "unauthorized_tool_calls": float(sum(item.unauthorized_tool_calls for item in ordered)),
        }
        threshold_results = {
            name: (
                metrics[name.removesuffix("_min")] >= threshold
                if name.endswith("_min")
                else metrics[name.removesuffix("_max")] <= threshold
            )
            for name, threshold in dataset.thresholds.items()
        }
        failed = tuple(
            item.scenario_id
            for item in ordered
            if item.unauthorized_tool_calls
            or not all(
                (
                    item.intent_correct,
                    item.required_fields_complete,
                    item.dag_valid,
                    item.evidence_gap_blocked,
                    item.recovery_success,
                    item.session_isolated,
                )
            )
        )
        return AgentEvaluationReport(
            dataset.name,
            dataset.fingerprint,
            len(ordered),
            metrics,
            threshold_results,
            failed,
            not failed and all(threshold_results.values()),
        )


def _mean(values) -> float:
    materialized = list(values)
    return round(sum(bool(item) for item in materialized) / len(materialized), 6)


def _average(values) -> float:
    materialized = list(values)
    return round(sum(float(item) for item in materialized) / len(materialized), 6)
