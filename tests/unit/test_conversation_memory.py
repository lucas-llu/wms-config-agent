import json
from dataclasses import replace

import pytest

from agents.conversation_memory import ContextLimitError, prepare_memory, resolve_followup
from core.settings import load_settings


def history(*messages):
    return [
        {"sequence": i, "role": "user" if i % 2 else "assistant", "content": text}
        for i, text in enumerate(messages, 1)
    ]


def prepare(turns, invoke, **kwargs):
    return prepare_memory(
        turns,
        summary=kwargs.pop("summary", ""),
        through=kwargs.pop("through", 0),
        confirmed=kwargs.pop("confirmed", {}),
        settings=replace(load_settings().agent, **kwargs),
        invoke=invoke,
    )


def test_recent_context_contains_both_sides_without_unnecessary_summary_call():
    def unexpected(*args):
        pytest.fail("Short conversation should not require summarization")

    result = prepare(
        history("Do not track slot ID", "Check slot type", "Where is that?"), unexpected
    )
    assert [t["role"] for t in result["conversation_recent"]] == ["user", "assistant", "user"]
    assert result["memory_through_sequence"] == 0
    assert "Do not track slot ID" in result["conversation_context"]


def test_rolling_summary_processes_only_unsummarized_turns_and_pins_requirements():
    prompts = []

    def summarize(prompt, validate):
        prompts.append(prompt)
        value = {
            "summary": "User needs slot picking without ID tracking; warehouse corrected to DC02."
        }
        validate(value)
        return value

    turns = history(
        "Do not track slot ID", "Check the handling unit", "Use DC02", "Noted", "Where?"
    )
    first = prepare(turns[:3], summarize, max_context_turns=2, confirmed={"site": "DC02"})
    second = prepare(
        turns,
        summarize,
        max_context_turns=2,
        summary=first["conversation_summary"],
        through=first["memory_through_sequence"],
        confirmed={"site": "DC02"},
    )
    assert first["memory_through_sequence"] == 1
    assert second["memory_through_sequence"] == 3
    assert '"content": "Do not track slot ID"' not in prompts[1]
    assert "Check the handling unit" in prompts[1]
    assert json.loads(second["conversation_context"])["confirmed_requirements"] == {"site": "DC02"}
    assert [t["content"] for t in second["conversation_recent"]] == ["Noted", "Where?"]
    assert len(turns) == 5  # Input history is never mutated.


def test_character_limit_compacts_before_count_limit_and_preserves_latest_input():
    def summarize(prompt, validate):
        value = {"summary": "Earlier conversation concerned slot picking."}
        validate(value)
        return value

    result = prepare(
        history("A" * 450, "B" * 450, "Latest request"),
        summarize,
        max_context_chars=900,
        max_summary_chars=100,
    )
    assert result["memory_through_sequence"] > 0
    assert len(result["conversation_context"]) <= 900
    assert result["conversation_recent"][-1]["content"] == "Latest request"


@pytest.mark.parametrize(
    "confirmed,message", [({}, "x" * 1000), ({"constraints": ["x" * 1000]}, "hi")]
)
def test_latest_input_or_pinned_requirements_cannot_be_silently_truncated(confirmed, message):
    with pytest.raises(ContextLimitError):
        prepare(
            history(message),
            lambda *args: pytest.fail("No request allowed"),
            confirmed=confirmed,
            max_context_chars=900,
            max_summary_chars=100,
        )


@pytest.mark.parametrize(
    "bad", [{"summary": ""}, {"summary": "x" * 101}, {"summary": "ok", "extra": True}]
)
def test_invalid_summary_rejected_without_mutating_history(bad):
    turns = history("first", "reply", "latest")
    before = json.dumps(turns)

    def invoke(prompt, validate):
        validate(bad)
        return bad

    with pytest.raises(ValueError):
        prepare(turns, invoke, max_context_turns=1, max_summary_chars=100)
    assert json.dumps(turns) == before


def test_followup_must_either_resolve_or_clarify_and_is_bounded():
    for value in (
        {"question": "", "clarification": ""},
        {"question": "a", "clarification": "b"},
        {"question": "x" * 4001, "clarification": ""},
    ):

        def invoke(prompt, validate, value=value):
            validate(value)
            return value

        with pytest.raises(ValueError):
            resolve_followup("Which one?", "{}", invoke)


def test_oversized_old_message_and_excessive_batches_stop_without_cursor_commit():
    with pytest.raises(ContextLimitError):
        prepare(
            history("x" * 1000, "latest"),
            lambda *args: {},
            max_context_turns=1,
            max_context_chars=900,
            max_summary_chars=100,
        )

    def invoke(prompt, validate):
        return {"summary": "Earlier facts"}

    with pytest.raises(ContextLimitError):
        prepare(
            history(*(["x" * 500] * 6), "latest"),
            invoke,
            max_context_turns=1,
            max_context_chars=900,
            max_summary_chars=100,
        )


def test_ambiguous_followup_clarification_must_use_the_conversation_language():
    def invoke(prompt, validate):
        value = {"question": "", "clarification": "Which option do you mean?"}
        validate(value)
        return value

    with pytest.raises(ValueError):
        resolve_followup("那个选项呢？", "{}", invoke, language="zh")
