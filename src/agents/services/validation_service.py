"""Deterministic conflict analysis and draft validation gates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agents.contracts import (
    ConfigurationConflict,
    EvidenceStatus,
    FindingSeverity,
    ValidationFinding,
    stable_contract_id,
)

_COMMAND = re.compile(
    r"\b(select|update|insert|delete|execute|exec|moca|sql)\b|[A-Z][A-Z0-9_]*\.[A-Z0-9_.]+", re.I
)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    conflicts: tuple[ConfigurationConflict, ...]
    findings: tuple[ValidationFinding, ...]
    targeted_requirements: dict[str, tuple[str, ...]]
    fingerprint: str

    @property
    def blocking(self) -> bool:
        return any(item.blocking for item in self.conflicts) or any(
            item.severity is FindingSeverity.BLOCKING for item in self.findings
        )


class ValidationService:
    def validate(
        self,
        *,
        tasks: list[dict[str, Any]],
        dependency_edges: list[dict[str, Any]],
        evidence_registry: list[dict[str, Any]],
        bindings: list[dict[str, Any]],
        confirmed_context: dict[str, Any],
        invalidated_task_ids: list[str],
    ) -> ValidationReport:
        task_by_id = {str(item.get("task_id", "")): item for item in tasks}
        evidence_by_id = {str(item.get("evidence_id", "")): item for item in evidence_registry}
        binding_by_task = {str(item.get("task_id", "")): item for item in bindings}
        conflicts = self._conflicts(task_by_id, evidence_by_id, binding_by_task, confirmed_context)
        findings = self._findings(
            task_by_id,
            dependency_edges,
            evidence_by_id,
            binding_by_task,
            invalidated_task_ids,
        )
        targeted = self._targeted_requirements(binding_by_task)
        fingerprint = stable_contract_id(
            "validation",
            {
                "conflicts": [item.to_dict() for item in conflicts],
                "findings": [item.to_dict() for item in findings],
            },
        )
        return ValidationReport(conflicts, findings, targeted, fingerprint)

    def _conflicts(
        self,
        tasks: dict[str, dict[str, Any]],
        evidence: dict[str, dict[str, Any]],
        bindings: dict[str, dict[str, Any]],
        context: dict[str, Any],
    ) -> tuple[ConfigurationConflict, ...]:
        expected_fields = {
            "product_version": _text(context.get("product_version")),
            "site": _text(context.get("site")),
            "environment": _text(context.get("environment")),
        }
        conflicts: list[ConfigurationConflict] = []
        for task_id in sorted(tasks):
            task = tasks[task_id]
            binding = bindings.get(task_id, {})
            evidence_ids = tuple(sorted(str(item) for item in binding.get("evidence_ids", [])))
            scoped = [evidence[item] for item in evidence_ids if item in evidence]
            dimensions = {
                **expected_fields,
                "module": _text(task.get("module")),
            }
            for dimension, expected in dimensions.items():
                actual = {
                    value for item in scoped if (value := _text(item.get(dimension))) is not None
                }
                mismatched = tuple(
                    sorted(
                        item_id
                        for item_id in evidence_ids
                        if item_id in evidence
                        and (value := _text(evidence[item_id].get(dimension))) is not None
                        and expected is not None
                        and value.casefold() != expected.casefold()
                    )
                )
                if len(actual) > 1 or mismatched:
                    conflict_evidence = evidence_ids if len(actual) > 1 else mismatched
                    conflicts.append(
                        _conflict(
                            dimension,
                            task_id,
                            conflict_evidence,
                            expected,
                            actual,
                        )
                    )
        return tuple(sorted(conflicts, key=lambda item: item.conflict_id))

    def _findings(
        self,
        tasks: dict[str, dict[str, Any]],
        edges: list[dict[str, Any]],
        evidence: dict[str, dict[str, Any]],
        bindings: dict[str, dict[str, Any]],
        invalidated_task_ids: list[str],
    ) -> tuple[ValidationFinding, ...]:
        findings: list[ValidationFinding] = []
        task_ids = set(tasks)
        expected_edges: set[tuple[str, str]] = set()
        for task_id, task in tasks.items():
            dependencies = tuple(str(item) for item in task.get("depends_on", []))
            for dependency in dependencies:
                expected_edges.add((dependency, task_id))
                if dependency not in task_ids:
                    findings.append(_finding("missing_dependency", task_id))
            if dependencies and not _items(task.get("preconditions")):
                findings.append(_finding("missing_preconditions", task_id))
            for field, rule in (
                ("steps", "missing_steps"),
                ("validation_steps", "missing_validation"),
                ("rollback_steps", "missing_rollback"),
                ("evidence_requirements", "missing_evidence_requirements"),
            ):
                if not _items(task.get(field)):
                    findings.append(_finding(rule, task_id))

            binding = bindings.get(task_id)
            if binding is None:
                findings.append(_finding("missing_evidence_binding", task_id))
                continue
            status = str(binding.get("evidence_status", "unsupported"))
            evidence_ids = tuple(str(item) for item in binding.get("evidence_ids", []))
            if status != EvidenceStatus.SUPPORTED.value:
                findings.append(_finding("evidence_coverage", task_id, evidence_ids))
            if any(item not in evidence for item in evidence_ids):
                findings.append(_finding("missing_evidence_reference", task_id, evidence_ids))
            if (
                any(_COMMAND.search(step) for step in _items(task.get("steps")))
                and status != EvidenceStatus.SUPPORTED.value
            ):
                findings.append(_finding("command_without_evidence", task_id, evidence_ids))
            if str(task.get("risk_level", "medium")) in {"high", "critical"} and not _items(
                task.get("rollback_steps")
            ):
                findings.append(_finding("high_risk_without_rollback", task_id))

        actual_edges = {
            (str(item.get("upstream_task_id", "")), str(item.get("downstream_task_id", "")))
            for item in edges
        }
        if actual_edges != expected_edges and tasks:
            findings.append(_finding("dependency_edge_mismatch", *sorted(task_ids)))
        if _has_cycle(tasks):
            findings.append(_finding("dependency_cycle", *sorted(task_ids)))
        if invalidated_task_ids:
            findings.append(_finding("invalidated_tasks", *sorted(invalidated_task_ids)))
        return tuple(sorted(_deduplicate(findings), key=lambda item: item.finding_id))

    @staticmethod
    def _targeted_requirements(
        bindings: dict[str, dict[str, Any]],
    ) -> dict[str, tuple[str, ...]]:
        targeted: dict[str, tuple[str, ...]] = {}
        for task_id, binding in sorted(bindings.items()):
            if str(binding.get("evidence_status")) == EvidenceStatus.SUPPORTED.value:
                continue
            available = {
                str(item.get("requirement", "")).strip()
                for item in binding.get("queries", [])
                if str(item.get("requirement", "")).strip()
            }
            gaps = tuple(str(item) for item in binding.get("gap_reasons", []))
            selected = {
                requirement for requirement in available if any(requirement in gap for gap in gaps)
            }
            requirements = tuple(sorted(selected or available))
            if requirements:
                targeted[task_id] = requirements
        return targeted


def _conflict(
    dimension: str,
    task_id: str,
    evidence_ids: tuple[str, ...],
    expected: str | None,
    actual: set[str],
) -> ConfigurationConflict:
    expected_label = expected or "unspecified"
    actual_label = ",".join(sorted(actual)) or "unspecified"
    summary = (
        f"Evidence scope conflict for {dimension}: expected={expected_label}, actual={actual_label}"
    )
    return ConfigurationConflict(
        conflict_id=stable_contract_id(
            "conflict",
            {"dimension": dimension, "task_id": task_id, "evidence_ids": evidence_ids},
        ),
        summary=summary,
        dimension=dimension,
        task_ids=(task_id,),
        evidence_ids=evidence_ids,
    )


def _finding(rule_id: str, *task_ids: str | tuple[str, ...]) -> ValidationFinding:
    flattened: list[str] = []
    evidence_ids: tuple[str, ...] = ()
    for value in task_ids:
        if isinstance(value, tuple):
            evidence_ids = value
        elif value:
            flattened.append(value)
    stable_tasks = tuple(sorted(set(flattened)))
    return ValidationFinding(
        finding_id=stable_contract_id(
            "finding",
            {"rule_id": rule_id, "task_ids": stable_tasks, "evidence_ids": evidence_ids},
        ),
        rule_id=rule_id,
        message=rule_id.replace("_", " "),
        severity=FindingSeverity.BLOCKING,
        task_ids=stable_tasks,
        evidence_ids=tuple(sorted(set(evidence_ids))),
    )


def _deduplicate(values: list[ValidationFinding]) -> tuple[ValidationFinding, ...]:
    return tuple({item.finding_id: item for item in values}.values())


def _has_cycle(tasks: dict[str, dict[str, Any]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        for dependency in tasks.get(task_id, {}).get("depends_on", []):
            if str(dependency) in tasks and visit(str(dependency)):
                return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    return any(visit(task_id) for task_id in sorted(tasks))


def _items(value: Any) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in value or [] if str(item).strip())


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
