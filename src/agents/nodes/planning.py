"""Structured Planning Agent backed by deterministic task-graph validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.contracts import ConfigurationTask, RiskLevel
from agents.llm_json import StructuredLLMError, invoke_json
from agents.task_graph import TaskDraft, TaskGraphError, TaskPlan, build_task_plan
from libs.llm import BaseLLM

_TASK_FIELDS = frozenset(
    {
        "task_key",
        "title",
        "module",
        "goal",
        "depends_on",
        "preconditions",
        "steps",
        "validation_steps",
        "rollback_steps",
        "evidence_requirements",
        "risk_level",
    }
)
_REQUIRED_LIST_FIELDS = (
    "steps",
    "validation_steps",
    "rollback_steps",
    "evidence_requirements",
)
_OPTIONAL_LIST_FIELDS = ("depends_on", "preconditions")


@dataclass(frozen=True, slots=True)
class PlanningResult:
    plan: TaskPlan
    retries: int
    tokens_used: int


class PlanningAgent:
    def __init__(
        self,
        llm: BaseLLM,
        *,
        max_retries: int,
        prompt_path: str | Path,
        template_path: str | Path,
    ) -> None:
        self.llm = llm
        self.max_retries = max_retries
        self.prompt = Path(prompt_path).read_text(encoding="utf-8")
        template = json.loads(Path(template_path).read_text(encoding="utf-8"))
        if not isinstance(template, dict):
            raise TaskGraphError("planning task template must be a JSON object")
        self.template = template

    def plan(
        self,
        *,
        user_goal: str,
        confirmed_context: dict[str, Any],
        assumptions: list[dict[str, Any]],
        previous_tasks: list[dict[str, Any]],
    ) -> PlanningResult:
        prior_tasks = _previous_tasks(previous_tasks)

        def validate(payload: dict[str, Any]) -> None:
            build_task_plan(
                _parse_drafts(payload),
                user_goal=user_goal,
                confirmed_context=confirmed_context,
                previous_tasks=prior_tasks,
            )

        invocation = invoke_json(
            self.llm,
            [
                {
                    "role": "user",
                    "content": self.prompt.replace("{user_goal}", user_goal)
                    .replace(
                        "{confirmed_context}",
                        json.dumps(confirmed_context, ensure_ascii=False, sort_keys=True),
                    )
                    .replace(
                        "{assumptions}",
                        json.dumps(assumptions, ensure_ascii=False, sort_keys=True),
                    )
                    .replace(
                        "{task_template}",
                        json.dumps(self.template, ensure_ascii=False, sort_keys=True),
                    ),
                }
            ],
            max_retries=self.max_retries,
            validator=validate,
        )
        try:
            plan = build_task_plan(
                _parse_drafts(invocation.payload),
                user_goal=user_goal,
                confirmed_context=confirmed_context,
                previous_tasks=prior_tasks,
            )
        except (TaskGraphError, ValueError) as exc:
            raise StructuredLLMError(
                str(exc),
                retries=invocation.retries,
                tokens_used=invocation.tokens_used,
            ) from exc
        return PlanningResult(plan, invocation.retries, invocation.tokens_used)


def _parse_drafts(payload: dict[str, Any]) -> tuple[TaskDraft, ...]:
    if set(payload) != {"tasks"}:
        raise TaskGraphError("planning output must contain only the top-level tasks field")
    values = payload["tasks"]
    if not isinstance(values, list) or not values:
        raise TaskGraphError("planning output tasks must be a non-empty list")
    return tuple(_parse_draft(value, index) for index, value in enumerate(values))


def _parse_draft(value: Any, index: int) -> TaskDraft:
    if not isinstance(value, dict):
        raise TaskGraphError(f"tasks[{index}] must be a JSON object")
    unknown = sorted(set(value) - _TASK_FIELDS)
    if unknown:
        raise TaskGraphError(f"tasks[{index}] contains unsupported fields: {', '.join(unknown)}")

    text = {
        field: _required_text(value, field, index)
        for field in ("task_key", "title", "module", "goal")
    }
    lists = {
        field: _string_tuple(value.get(field, []), field, index, required=False)
        for field in _OPTIONAL_LIST_FIELDS
    }
    lists.update(
        {
            field: _string_tuple(value.get(field), field, index, required=True)
            for field in _REQUIRED_LIST_FIELDS
        }
    )
    try:
        risk_level = RiskLevel(str(value.get("risk_level", RiskLevel.MEDIUM.value)))
    except ValueError as exc:
        raise TaskGraphError(f"tasks[{index}].risk_level is invalid") from exc
    return TaskDraft(**text, **lists, risk_level=risk_level)


def _required_text(value: dict[str, Any], field: str, index: int) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise TaskGraphError(f"tasks[{index}].{field} must be a non-empty string")
    return item.strip()


def _string_tuple(value: Any, field: str, index: int, *, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise TaskGraphError(f"tasks[{index}].{field} must be a list of non-empty strings")
    if required and not value:
        raise TaskGraphError(f"tasks[{index}].{field} must not be empty")
    return tuple(item.strip() for item in value)


def _previous_tasks(values: list[dict[str, Any]]) -> tuple[ConfigurationTask, ...]:
    tasks: list[ConfigurationTask] = []
    for index, value in enumerate(values):
        try:
            tasks.append(
                ConfigurationTask(
                    task_id=str(value["task_id"]),
                    title=str(value["title"]),
                    module=str(value["module"]),
                    goal=str(value["goal"]),
                    baseline_fingerprint=str(value.get("baseline_fingerprint", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskGraphError(f"previous_tasks[{index}] is invalid") from exc
    return tuple(tasks)
