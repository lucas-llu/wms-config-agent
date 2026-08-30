"""Deterministic construction and validation of configuration task DAGs."""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from typing import Any

from agents.contracts import (
    AgentContractError,
    ConfigurationTask,
    DependencyEdge,
    RiskLevel,
    TaskStatus,
    stable_contract_id,
)

_TASK_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class TaskGraphError(AgentContractError):
    """Raised when a proposed task graph is incomplete or non-deterministic."""


@dataclass(frozen=True, slots=True)
class TaskDraft:
    task_key: str
    title: str
    module: str
    goal: str
    depends_on: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    validation_steps: tuple[str, ...] = ()
    rollback_steps: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.MEDIUM

    def __post_init__(self) -> None:
        if not _TASK_KEY.fullmatch(self.task_key):
            raise TaskGraphError(
                "task_key must be lowercase snake_case and at most 64 characters"
            )
        for field_name in ("title", "module", "goal"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise TaskGraphError(f"TaskDraft.{field_name} must be non-empty")
        for field_name in (
            "depends_on",
            "preconditions",
            "steps",
            "validation_steps",
            "rollback_steps",
            "evidence_requirements",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise TaskGraphError(
                    f"TaskDraft.{field_name} must be a tuple of non-empty strings"
                )
        if not isinstance(self.risk_level, RiskLevel):
            raise TaskGraphError("TaskDraft.risk_level must be a RiskLevel")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise TaskGraphError(
                f"task {self.task_key!r} contains duplicate dependencies"
            )
        if self.task_key in self.depends_on:
            raise TaskGraphError(f"task {self.task_key!r} cannot depend on itself")


@dataclass(frozen=True, slots=True)
class TaskPlan:
    tasks: tuple[ConfigurationTask, ...]
    edges: tuple[DependencyEdge, ...]
    baseline_fingerprint: str
    invalidated_task_ids: tuple[str, ...]


def build_task_plan(
    drafts: tuple[TaskDraft, ...],
    *,
    user_goal: str,
    confirmed_context: dict[str, Any],
    previous_tasks: tuple[ConfigurationTask, ...] = (),
) -> TaskPlan:
    """Validate, deduplicate, sort, and materialize an LLM-proposed task graph."""

    if not isinstance(user_goal, str) or not user_goal.strip():
        raise TaskGraphError("user_goal must be non-empty")
    if not drafts:
        raise TaskGraphError("planning output must contain at least one task")

    by_key: dict[str, TaskDraft] = {}
    semantic_keys: dict[tuple[str, str], str] = {}
    for draft in drafts:
        if draft.task_key in by_key:
            raise TaskGraphError(f"duplicate task_key: {draft.task_key}")
        semantic_key = (_normalize(draft.module), _normalize(draft.goal))
        duplicate = semantic_keys.get(semantic_key)
        if duplicate is not None:
            raise TaskGraphError(
                f"duplicate semantic task: {duplicate} and {draft.task_key}"
            )
        by_key[draft.task_key] = draft
        semantic_keys[semantic_key] = draft.task_key

    for draft in drafts:
        missing = sorted(set(draft.depends_on) - set(by_key))
        if missing:
            raise TaskGraphError(
                f"task {draft.task_key!r} references missing dependencies: "
                f"{', '.join(missing)}"
            )

    ordered_keys = _stable_topological_order(by_key)
    baseline_fingerprint = stable_contract_id(
        "baseline",
        {"user_goal": user_goal.strip(), "confirmed_context": confirmed_context},
    )
    identifiers = {
        key: stable_contract_id(
            "task",
            {"task_key": key, "module": _normalize(by_key[key].module)},
        )
        for key in by_key
    }

    tasks = tuple(
        _materialize_task(
            by_key[key],
            identifiers=identifiers,
            baseline_fingerprint=baseline_fingerprint,
        )
        for key in ordered_keys
    )
    edges = tuple(
        DependencyEdge(
            upstream_task_id=identifiers[dependency],
            downstream_task_id=identifiers[key],
            reason="declared_planning_dependency",
        )
        for key in ordered_keys
        for dependency in sorted(by_key[key].depends_on)
    )
    invalidated_task_ids = tuple(
        sorted(
            task.task_id
            for task in previous_tasks
            if task.baseline_fingerprint != baseline_fingerprint
        )
    )
    return TaskPlan(tasks, edges, baseline_fingerprint, invalidated_task_ids)


def _stable_topological_order(tasks: dict[str, TaskDraft]) -> tuple[str, ...]:
    indegree = {key: len(set(task.depends_on)) for key, task in tasks.items()}
    dependents: dict[str, set[str]] = {key: set() for key in tasks}
    for key, task in tasks.items():
        for dependency in task.depends_on:
            dependents[dependency].add(key)

    ready = [key for key, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        key = heapq.heappop(ready)
        ordered.append(key)
        for dependent in sorted(dependents[key]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)

    if len(ordered) != len(tasks):
        cyclic = ", ".join(sorted(key for key, degree in indegree.items() if degree > 0))
        raise TaskGraphError(f"task dependency cycle detected: {cyclic}")
    return tuple(ordered)


def _materialize_task(
    draft: TaskDraft,
    *,
    identifiers: dict[str, str],
    baseline_fingerprint: str,
) -> ConfigurationTask:
    return ConfigurationTask(
        task_id=identifiers[draft.task_key],
        title=draft.title.strip(),
        module=draft.module.strip(),
        goal=draft.goal.strip(),
        status=TaskStatus.DRAFT,
        depends_on=tuple(sorted(identifiers[key] for key in set(draft.depends_on))),
        preconditions=_unique(draft.preconditions),
        steps=_unique(draft.steps),
        validation_steps=_unique(draft.validation_steps),
        rollback_steps=_unique(draft.rollback_steps),
        evidence_requirements=_unique(draft.evidence_requirements),
        risk_level=draft.risk_level,
        baseline_fingerprint=baseline_fingerprint,
    )


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in values))
