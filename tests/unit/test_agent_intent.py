from __future__ import annotations

import json

import pytest

from agents.contracts import IntentType
from agents.llm_json import StructuredLLMError
from agents.nodes import IntentClassifier
from libs.llm import ChatResponse


class ScriptedLLM:
    model = "fake-intent"

    def __init__(self, *outputs: dict[str, object] | str) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    def chat(self, messages, trace=None) -> ChatResponse:
        del messages, trace
        self.calls += 1
        output = self.outputs.pop(0)
        content = json.dumps(output) if isinstance(output, dict) else output
        return ChatResponse(content, model=self.model, metadata={"usage": {"total_tokens": 11}})


def _classifier(llm: ScriptedLLM | None = None) -> IntentClassifier:
    return IntentClassifier(
        llm or ScriptedLLM(),
        confidence_threshold=0.65,
        max_retries=2,
        prompt_path="config/prompts/agent_intent.txt",
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Where is the appointment capacity configured?", IntentType.ATOMIC_QUERY),
        ("SWL.I.01.01 是什么？", IntentType.ATOMIC_QUERY),
        ("怎么查询 MOCA policy？", IntentType.ATOMIC_QUERY),
        ("Build a complete inbound appointment configuration plan", IntentType.CONFIGURE_GOAL),
        ("帮我设计新仓的收货预约全套方案", IntentType.CONFIGURE_GOAL),
        ("请帮我配置 DC01 的入库流程，可以吗？", IntentType.CONFIGURE_GOAL),
        ("Implement receiving configuration for DC01", IntentType.CONFIGURE_GOAL),
        ("Show the current configuration draft", IntentType.INSPECT_DRAFT),
        ("查看当前方案草案", IntentType.INSPECT_DRAFT),
        ("Tell me a joke about the weather", IntentType.UNSUPPORTED),
        ("今天天气怎么样？", IntentType.UNSUPPORTED),
    ],
)
def test_rule_intent_dataset(message: str, expected: IntentType) -> None:
    llm = ScriptedLLM()

    result = _classifier(llm).classify(message)

    assert result.intent is expected
    assert result.confidence == 1.0
    assert llm.calls == 0


def test_ambiguous_intent_uses_structured_llm_and_requests_clarification() -> None:
    llm = ScriptedLLM(
        {"intent": "configure_goal", "confidence": 0.4, "reason": "ambiguous request"}
    )
    classifier = _classifier(llm)

    result = classifier.classify("I need help with the warehouse")

    assert result.intent is IntentType.CONFIGURE_GOAL
    assert result.tokens_used == 11
    assert classifier.requires_clarification(result) is True


def test_invalid_semantic_intent_payload_fails_closed() -> None:
    invalid = {"intent": "invented", "confidence": 0.9, "reason": "invalid"}
    llm = ScriptedLLM(invalid, invalid, invalid)
    classifier = _classifier(llm)

    with pytest.raises(StructuredLLMError, match="invalid intent"):
        classifier.classify("warehouse assistance")
    assert llm.calls == 3
