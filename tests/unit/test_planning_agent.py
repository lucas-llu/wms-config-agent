from __future__ import annotations

import json

import pytest

from agents.llm_json import StructuredLLMError
from agents.nodes import PlanningAgent
from libs.llm import ChatResponse


class ScriptedLLM:
    model = "fake-planning"

    def __init__(self, *outputs: dict[str, object] | str) -> None:
        self.outputs = list(outputs)
        self.messages = []

    def chat(self, messages, trace=None) -> ChatResponse:
        del trace
        self.messages.append(messages)
        output = self.outputs.pop(0)
        content = json.dumps(output) if isinstance(output, dict) else output
        return ChatResponse(content, metadata={"usage": {"total_tokens": 23}})


def _valid_output() -> dict[str, object]:
    return {
        "tasks": [
            {
                "task_key": "confirm_scope",
                "title": "Confirm inbound scope",
                "module": "inbound",
                "goal": "Confirm the receiving scope",
                "depends_on": [],
                "preconditions": [],
                "steps": ["Review the confirmed requirement baseline"],
                "validation_steps": ["Confirm site and environment are explicit"],
                "rollback_steps": ["Return the task to draft"],
                "evidence_requirements": ["Version-matched receiving documentation"],
                "risk_level": "low",
            },
            {
                "task_key": "configure_capacity",
                "title": "Plan appointment capacity",
                "module": "appointment",
                "goal": "Define appointment capacity behavior",
                "depends_on": ["confirm_scope"],
                "preconditions": ["Inbound scope confirmed"],
                "steps": ["Describe the required capacity behavior"],
                "validation_steps": ["Verify the planned behavior against requirements"],
                "rollback_steps": ["Restore the previous capacity plan"],
                "evidence_requirements": ["Appointment capacity documentation"],
                "risk_level": "medium",
            },
        ]
    }


def _agent(llm: ScriptedLLM) -> PlanningAgent:
    return PlanningAgent(
        llm,
        max_retries=2,
        prompt_path="config/prompts/agent_planning.txt",
        template_path="config/templates/inbound_appointment_receiving.json",
    )


def test_planning_agent_builds_validated_dag_and_includes_template_prior() -> None:
    llm = ScriptedLLM(_valid_output())

    result = _agent(llm).plan(
        user_goal="Configure inbound appointments",
        confirmed_context={"site": "DC01", "environment": "test"},
        assumptions=[],
        previous_tasks=[],
    )

    assert [task.title for task in result.plan.tasks] == [
        "Confirm inbound scope",
        "Plan appointment capacity",
    ]
    assert result.plan.tasks[1].depends_on == (result.plan.tasks[0].task_id,)
    assert result.plan.edges
    assert "inbound_appointment_receiving_v1" in llm.messages[0][0]["content"]


def test_semantic_graph_error_is_retried_before_accepting_valid_plan() -> None:
    cyclic = _valid_output()
    tasks = cyclic["tasks"]
    assert isinstance(tasks, list)
    tasks[0]["depends_on"] = ["configure_capacity"]  # type: ignore[index]
    llm = ScriptedLLM(cyclic, _valid_output())

    result = _agent(llm).plan(
        user_goal="Configure inbound appointments",
        confirmed_context={"site": "DC01"},
        assumptions=[],
        previous_tasks=[],
    )

    assert result.retries == 1


def test_exhausted_invalid_planning_outputs_fail_closed() -> None:
    invalid = {"tasks": [{"task_key": "missing_contract_fields"}]}
    llm = ScriptedLLM(invalid, invalid, invalid)

    with pytest.raises(StructuredLLMError, match="title"):
        _agent(llm).plan(
            user_goal="Configure inbound appointments",
            confirmed_context={"site": "DC01"},
            assumptions=[],
            previous_tasks=[],
        )
