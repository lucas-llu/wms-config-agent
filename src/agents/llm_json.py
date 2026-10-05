"""Strict JSON invocation helpers shared by Agent LLM nodes."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from libs.llm import BaseLLM, ChatMessage


class StructuredLLMError(RuntimeError):
    """Raised when every bounded structured-output attempt fails."""

    def __init__(self, message: str, *, retries: int, tokens_used: int) -> None:
        super().__init__(message)
        self.retries = retries
        self.tokens_used = tokens_used


@dataclass(frozen=True, slots=True)
class JSONInvocation:
    payload: dict[str, Any]
    retries: int
    tokens_used: int


def invoke_json(
    llm: BaseLLM,
    messages: list[ChatMessage],
    *,
    max_retries: int,
    validator: Callable[[dict[str, Any]], None] | None = None,
) -> JSONInvocation:
    """Invoke an LLM with bounded retries and require a top-level JSON object."""

    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    retries = 0
    tokens_used = 0
    last_error = "unknown structured output failure"
    for attempt in range(max_retries + 1):
        try:
            response = llm.chat(messages)
            tokens_used += _response_tokens(messages, response.content, response.metadata)
            payload = json.loads(response.content)
            if not isinstance(payload, dict):
                raise ValueError("structured response must be a JSON object")
            if validator is not None:
                validator(payload)
            return JSONInvocation(payload, retries, tokens_used)
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == max_retries:
                break
            retries += 1
    raise StructuredLLMError(last_error, retries=retries, tokens_used=tokens_used)


def _response_tokens(
    messages: list[ChatMessage],
    content: str,
    metadata: dict[str, Any],
) -> int:
    usage = metadata.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            return total
    total = metadata.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    characters = sum(len(item.get("content", "")) for item in messages) + len(content)
    return max(1, math.ceil(characters / 4))
