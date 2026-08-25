"""OpenAI Chat Completions compatible text provider."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.settings import ProviderSettings
from libs.llm.base_llm import BaseLLM, ChatMessage, ChatResponse

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
JsonTransport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]
Sleeper = Callable[[float], None]


class LLMConfigurationError(RuntimeError):
    """Raised when a provider cannot run with the local configuration."""


class LLMProviderError(RuntimeError):
    """Raised for sanitized remote protocol and transport failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class OpenAICompatibleLLM(BaseLLM):
    """Call a provider implementing the OpenAI Chat Completions response shape."""

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        transport: JsonTransport | None = None,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        if not settings.model or not settings.model.strip():
            raise LLMConfigurationError("OpenAI-compatible LLM requires a model")
        if not settings.base_url or not settings.base_url.strip():
            raise LLMConfigurationError("OpenAI-compatible LLM requires a base_url")
        self.model = settings.model.strip()
        self.endpoint = _chat_completions_endpoint(settings.base_url)
        self.api_key_env = settings.api_key_env
        self.timeout_seconds = settings.timeout_seconds
        self.max_tokens = settings.max_tokens
        self.temperature = settings.temperature
        self.max_retries = settings.max_retries
        self.retry_backoff_seconds = settings.retry_backoff_seconds
        self._transport = transport or _post_json
        self._sleep = sleeper

    def chat(
        self,
        messages: list[ChatMessage],
        trace: Any | None = None,
    ) -> ChatResponse:
        _validate_messages(messages)
        started = time.perf_counter()
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "wms-config-agent/0.1",
            }
            if self.api_key_env:
                api_key = os.getenv(self.api_key_env, "").strip()
                if not api_key:
                    raise LLMConfigurationError(
                        f"Environment variable {self.api_key_env} is not set"
                    )
                headers["Authorization"] = f"Bearer {api_key}"
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "stream": False,
            }
            response = self._request_with_retry(payload, headers)
            normalized = _normalize_response(response, requested_model=self.model)
        except Exception as exc:
            _record_trace(trace, started, self.model, "error", type(exc).__name__)
            raise
        _record_trace(trace, started, normalized.model or self.model, "ok")
        return normalized

    def _request_with_retry(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                return self._transport(
                    self.endpoint,
                    payload,
                    headers,
                    self.timeout_seconds,
                )
            except LLMProviderError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                self._sleep(self.retry_backoff_seconds * (2**attempt))
        raise AssertionError("unreachable retry state")


def _chat_completions_endpoint(base_url: str) -> str:
    endpoint = base_url.strip().rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint
    return f"{endpoint}/chat/completions"


def _validate_messages(messages: list[ChatMessage]) -> None:
    if not messages:
        raise ValueError("messages must not be empty")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise TypeError(f"messages[{index}] must be a mapping")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"messages[{index}].role must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"messages[{index}].content must be a non-empty string")


def _post_json(
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise LLMProviderError(
            f"LLM provider returned HTTP {exc.code}",
            status_code=exc.code,
            retryable=exc.code in {429, 500, 502, 503, 504},
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise LLMProviderError(
            f"LLM provider request failed: {type(exc).__name__}", retryable=True
        ) from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise LLMProviderError("LLM provider response exceeded the size limit")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LLMProviderError("LLM provider returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise LLMProviderError("LLM provider response must be a JSON object")
    return decoded


def _normalize_response(payload: dict[str, Any], *, requested_model: str) -> ChatResponse:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMProviderError("LLM provider response has no choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise LLMProviderError("LLM provider response has no assistant message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMProviderError("LLM provider returned empty assistant content")
    model = payload.get("model")
    usage = payload.get("usage")
    metadata: dict[str, Any] = {
        "provider": "openai_compatible",
        "finish_reason": choice.get("finish_reason"),
    }
    if isinstance(usage, dict):
        metadata["usage"] = usage
    return ChatResponse(
        content=content.strip(),
        model=model if isinstance(model, str) and model else requested_model,
        metadata=metadata,
    )


def _record_trace(
    trace: Any | None,
    started: float,
    model: str,
    status: str,
    error_type: str | None = None,
) -> None:
    if trace is None or not hasattr(trace, "record_stage"):
        return
    details = {"provider": "openai_compatible", "model": model, "status": status}
    if error_type:
        details["error_type"] = error_type
    trace.record_stage(
        "llm.chat",
        (time.perf_counter() - started) * 1000,
        details=details,
    )
