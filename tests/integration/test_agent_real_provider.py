from __future__ import annotations

import os

import pytest

from agents.nodes import IntentClassifier
from core.settings import load_settings
from libs.llm import LLMFactory


@pytest.mark.integration
def test_configured_real_provider_returns_valid_agent_intent() -> None:
    if os.environ.get("WMS_AGENT_LIVE") != "1":
        pytest.skip("set WMS_AGENT_LIVE=1 to run the Agent real-provider acceptance")
    settings = load_settings()
    classifier = IntentClassifier(
        LLMFactory.create(settings),
        confidence_threshold=settings.agent.intent_confidence_threshold,
        max_retries=settings.agent.max_self_repair_rounds,
        prompt_path=settings.agent.intent_prompt_path,
    )

    result = classifier.classify("I need help planning a warehouse configuration")

    assert 0 <= result.confidence <= 1
    assert result.reason
