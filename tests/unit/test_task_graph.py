from __future__ import annotations

from dataclasses import replace

import pytest

from agents.contracts import TaskStatus
from agents.task_graph import TaskDraft, TaskGraphError, build_task_plan


def _draft(
    task_key: str,
    *,
    module: str = "inbound",
    goal: str | None = None,
    depends_on: tuple[str, ...] = (),
) -> TaskDraft:
    return TaskDraft(
        task_key=task_key,
        title=task_key.replace("_", " ").title(),
        module=module,
        goal=goal or f"Complete {task_key}",
        depends_on=depends_on,
        preconditions=("Confirmed site and environment",),
        steps=(f"Plan {task_key}",),
        validation_steps=(f"Validate {task_key}",),
        rollback_steps=(f"Rollback {task_key}",),
        evidence_requirements=(f"Evidence for {task_key}",),
    )


def _plan(*drafts: TaskDraft, previous_tasks=()):
    return build_task_plan(
        tuple(drafts),
        user_goal="Configure inbound appointments",
        confirmed_context={"site": "DC01", "environment": "test"},
        previous_tasks=previous_tasks,
    )


def test_task_ids_and_topological_order_are_stable_across_input_order() -> None:
    first = _draft("confirm_scope")
    second = _draft("configure_capacity", depends_on=("confirm_scope",))

    forward = _plan(first, second)
    reverse = _plan(second, first)

    assert [task.task_id for task in forward.tasks] == [
        task.task_id for task in reverse.tasks
    ]
    assert [task.title for task in forward.tasks] == [
        "Confirm Scope",
        "Configure Capacity",
    ]
    assert forward.tasks[1].depends_on == (forward.tasks[0].task_id,)
    assert forward.edges[0].upstream_task_id == forward.tasks[0].task_id
    assert forward.edges[0].downstream_task_id == forward.tasks[1].task_id


def test_cycle_and_missing_dependency_are_rejected_deterministically() -> None:
    with pytest.raises(TaskGraphError, match="cycle detected"):
        _plan(
            _draft("first", depends_on=("second",)),
            _draft("second", depends_on=("first",)),
        )

    with pytest.raises(TaskGraphError, match="missing dependencies: unknown"):
        _plan(_draft("first", depends_on=("unknown",)))


def test_duplicate_keys_and_semantic_tasks_are_rejected() -> None:
    with pytest.raises(TaskGraphError, match="duplicate task_key"):
        _plan(_draft("same"), _draft("same", goal="Different wording"))

    with pytest.raises(TaskGraphError, match="duplicate semantic task"):
        _plan(
            _draft("first", module="Inbound", goal="Configure capacity"),
            _draft("second", module="inbound", goal="  configure   capacity "),
        )


def test_requirement_baseline_change_marks_previous_tasks_invalidated() -> None:
    original = _plan(_draft("confirm_scope"))
    unchanged = _plan(*(_draft("confirm_scope"),), previous_tasks=original.tasks)
    changed = build_task_plan(
        (_draft("confirm_scope"),),
        user_goal="Configure inbound appointments",
        confirmed_context={"site": "DC02", "environment": "test"},
        previous_tasks=original.tasks,
    )

    assert unchanged.invalidated_task_ids == ()
    assert changed.invalidated_task_ids == (original.tasks[0].task_id,)
    assert changed.tasks[0].status is TaskStatus.DRAFT
    assert changed.tasks[0].baseline_fingerprint != original.tasks[0].baseline_fingerprint


def test_required_planning_fields_are_preserved_and_deduplicated() -> None:
    draft = replace(
        _draft("configure_capacity"),
        steps=("Plan capacity", "Plan capacity"),
        evidence_requirements=("Capacity documentation", "Capacity documentation"),
    )

    task = _plan(draft).tasks[0]

    assert task.steps == ("Plan capacity",)
    assert task.evidence_requirements == ("Capacity documentation",)
    assert task.validation_steps
    assert task.rollback_steps
