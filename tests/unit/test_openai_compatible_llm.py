from __future__ import annotations

from typing import Any

import pytest

from core.settings import ProviderSettings
from core.trace import TraceContext
from libs.llm import (
    LLMConfigurationError,
    LLMProviderError,
    OpenAICompatibleLLM,
)


def _settings(**overrides: Any) -> ProviderSettings:
    values = {
        "provider": "openai_compatible",
        "model": "test-model",
        "base_url": "https://provider.example/v1",
        "api_key_env": "TEST_LLM_API_KEY",
        "timeout_seconds": 12.5,
        "max_tokens": 321,
        "temperature": 0.2,
        "max_retries": 2,
        "retry_backoff_seconds": 0.25,
    }
    values.update(overrides)
    return ProviderSettings(**values)


def test_chat_sends_compatible_payload_and_normalizes_response(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "unit-test-key")
    captured: dict[str, Any] = {}

    def transport(endpoint, payload, headers, timeout):
        captured.update(
            endpoint=endpoint,
            payload=payload,
            headers=headers,
            timeout=timeout,
        )
        return {
            "model": "served-model",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "  refined content  "},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }

    trace = TraceContext("integration")
    llm = OpenAICompatibleLLM(_settings(), transport=transport)
    response = llm.chat([{"role": "user", "content": "hello"}], trace=trace)

    assert captured["endpoint"] == "https://provider.example/v1/chat/completions"
    assert captured["payload"] == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.2,
        "max_tokens": 321,
        "stream": False,
    }
    assert captured["headers"]["Authorization"] == "Bearer unit-test-key"
    assert captured["headers"]["User-Agent"] == "wms-config-agent/0.1"
    assert captured["timeout"] == 12.5
    assert response.content == "refined content"
    assert response.model == "served-model"
    assert response.metadata["finish_reason"] == "stop"
    assert response.metadata["usage"]["completion_tokens"] == 2
    assert trace.to_dict()["stages"][0]["name"] == "llm.chat"
    assert trace.to_dict()["stages"][0]["details"]["status"] == "ok"


def test_full_chat_completions_endpoint_is_not_duplicated(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "unit-test-key")
    endpoints: list[str] = []

    def transport(endpoint, payload, headers, timeout):
        del payload, headers, timeout
        endpoints.append(endpoint)
        return {"choices": [{"message": {"content": "ok"}}]}

    llm = OpenAICompatibleLLM(
        _settings(base_url="https://provider.example/v1/chat/completions"),
        transport=transport,
    )

    llm.chat([{"role": "user", "content": "hello"}])

    assert endpoints == ["https://provider.example/v1/chat/completions"]


def test_missing_key_fails_before_network_and_records_sanitized_trace(monkeypatch) -> None:
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    called = False

    def transport(endpoint, payload, headers, timeout):
        nonlocal called
        del endpoint, payload, headers, timeout
        called = True
        return {}

    trace = TraceContext("integration")
    llm = OpenAICompatibleLLM(_settings(), transport=transport)

    with pytest.raises(LLMConfigurationError, match="TEST_LLM_API_KEY"):
        llm.chat([{"role": "user", "content": "hello"}], trace=trace)

    assert called is False
    details = trace.to_dict()["stages"][0]["details"]
    assert details == {
        "provider": "openai_compatible",
        "model": "test-model",
        "status": "error",
        "error_type": "LLMConfigurationError",
    }


def test_retryable_provider_failures_use_bounded_exponential_backoff(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "unit-test-key")
    attempts = 0
    delays: list[float] = []

    def transport(endpoint, payload, headers, timeout):
        nonlocal attempts
        del endpoint, payload, headers, timeout
        attempts += 1
        if attempts < 3:
            raise LLMProviderError(
                "LLM provider returned HTTP 503",
                status_code=503,
                retryable=True,
            )
        return {"choices": [{"message": {"content": "ok"}}]}

    llm = OpenAICompatibleLLM(_settings(), transport=transport, sleeper=delays.append)

    assert llm.chat([{"role": "user", "content": "hello"}]).content == "ok"
    assert attempts == 3
    assert delays == [0.25, 0.5]


def test_non_retryable_provider_failure_is_not_retried(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "unit-test-key")
    attempts = 0

    def transport(endpoint, payload, headers, timeout):
        nonlocal attempts
        del endpoint, payload, headers, timeout
        attempts += 1
        raise LLMProviderError(
            "LLM provider returned HTTP 401",
            status_code=401,
            retryable=False,
        )

    llm = OpenAICompatibleLLM(_settings(), transport=transport, sleeper=lambda delay: None)

    with pytest.raises(LLMProviderError, match="HTTP 401"):
        llm.chat([{"role": "user", "content": "hello"}])
    assert attempts == 1


@pytest.mark.parametrize(
    "response, message",
    [
        ({}, "no choices"),
        ({"choices": []}, "no choices"),
        ({"choices": [{}]}, "no assistant message"),
        ({"choices": [{"message": {"content": "  "}}]}, "empty assistant content"),
    ],
)
def test_invalid_provider_responses_are_rejected(monkeypatch, response, message) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "unit-test-key")
    llm = OpenAICompatibleLLM(
        _settings(),
        transport=lambda endpoint, payload, headers, timeout: response,
    )

    with pytest.raises(LLMProviderError, match=message):
        llm.chat([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize(
    "messages, message",
    [
        ([], "must not be empty"),
        ([{"role": "", "content": "hello"}], "role"),
        ([{"role": "user", "content": ""}], "content"),
    ],
)
def test_invalid_messages_are_rejected_without_network(messages, message) -> None:
    llm = OpenAICompatibleLLM(_settings(), transport=lambda *args: {})

    with pytest.raises(ValueError, match=message):
        llm.chat(messages)
