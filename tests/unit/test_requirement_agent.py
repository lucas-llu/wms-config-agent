from __future__ import annotations

import json

from agents.nodes import RequirementAgent
from libs.llm import ChatResponse


class ScriptedLLM:
    model = "fake-requirements"

    def __init__(self, *outputs: dict[str, object] | str) -> None:
        self.outputs = list(outputs)
        self.messages = []

    def chat(self, messages, trace=None) -> ChatResponse:
        del trace
        self.messages.append(messages)
        output = self.outputs.pop(0)
        content = json.dumps(output) if isinstance(output, dict) else output
        return ChatResponse(content, metadata={"usage": {"total_tokens": 17}})


def _agent(llm: ScriptedLLM, *, max_questions: int = 3) -> RequirementAgent:
    return RequirementAgent(
        llm,
        max_retries=2,
        max_questions=max_questions,
        prompt_path="config/prompts/agent_requirement.txt",
    )


def test_confirmed_fields_are_not_reasked_and_assumptions_stay_unconfirmed() -> None:
    agent = _agent(
        ScriptedLLM(
            {
                "confirmed_context": {
                    "business_process": "Inbound appointment",
                    "modules": ["inbound"],
                },
                "assumptions": ["The default volume profile may be sufficient"],
                "summary": "Configure inbound appointments",
            }
        )
    )

    result = agent.extract(
        user_message="Configure inbound appointments",
        turn_id="turn:one",
        confirmed_context={"product_version": "2024.1"},
        recent_turns=[{"role": "user", "content": "Configure inbound appointments"}],
    )

    question_reasons = {item.reason for item in result.open_questions}
    assert result.confirmed_context["product_version"] == "2024.1"
    assert "required_context_missing:product_version" not in question_reasons
    assert len(result.open_questions) <= 3
    assert result.assumptions[0].confirmed is False
    assert "volume_profile" not in result.confirmed_context


def test_requirement_context_merges_unique_list_values() -> None:
    llm = ScriptedLLM(
        {
            "confirmed_context": {
                "modules": ["inbound", "inventory"],
                "integrations": ["TMS"],
                "site": "DC01",
            },
            "assumptions": [],
            "summary": "Expanded scope",
        }
    )
    agent = _agent(llm)

    result = agent.extract(
        user_message="Include inventory and TMS at DC01",
        turn_id="turn:two",
        confirmed_context={"modules": ["inbound"], "integrations": ["ERP"]},
        recent_turns=[],
        requirement_summary="Initial receiving scope",
    )

    assert result.confirmed_context["modules"] == ["inbound", "inventory"]
    assert result.confirmed_context["integrations"] == ["ERP", "TMS"]
    assert "Initial receiving scope" in llm.messages[0][0]["content"]


def test_requirement_json_retries_are_bounded() -> None:
    agent = _agent(
        ScriptedLLM(
            "not-json",
            "still-not-json",
            {
                "confirmed_context": {},
                "assumptions": [],
                "summary": "Recovered output",
            },
        )
    )

    result = agent.extract(
        user_message="Configure receiving",
        turn_id="turn:retry",
        confirmed_context={},
        recent_turns=[],
    )

    assert result.retries == 2
    assert result.summary == "Recovered output"


def test_requirement_semantic_errors_are_retried() -> None:
    agent = _agent(
        ScriptedLLM(
            {"confirmed_context": {"modules": "inbound"}, "assumptions": []},
            {
                "confirmed_context": {"modules": ["inbound"]},
                "assumptions": [],
                "summary": "Recovered semantic output",
            },
        )
    )

    result = agent.extract(
        user_message="Configure inbound",
        turn_id="turn:semantic-retry",
        confirmed_context={},
        recent_turns=[],
    )

    assert result.retries == 1
    assert result.confirmed_context["modules"] == ["inbound"]


def test_null_summary_falls_back_to_latest_user_message() -> None:
    agent = _agent(
        ScriptedLLM(
            {"confirmed_context": {}, "assumptions": [], "summary": None},
        )
    )

    result = agent.extract(
        user_message="Configure receiving",
        turn_id="turn:null-summary",
        confirmed_context={},
        recent_turns=[],
    )

    assert result.summary == "Configure receiving"
