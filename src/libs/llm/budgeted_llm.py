"""Thread-safe logical-call budget for ingestion-time LLM use."""

from __future__ import annotations

from threading import Lock
from typing import Any

from libs.llm.base_llm import BaseLLM, ChatMessage, ChatResponse


class LLMBudgetExceeded(RuntimeError):
    """Raised before a request when the configured logical-call budget is exhausted."""


class BudgetedLLM(BaseLLM):
    """Share one bounded call budget across multiple transforms."""

    def __init__(self, delegate: BaseLLM, max_calls: int) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be greater than 0")
        self.delegate = delegate
        self.max_calls = max_calls
        self.model = getattr(delegate, "model", None)
        self._calls_made = 0
        self._lock = Lock()

    @property
    def calls_made(self) -> int:
        with self._lock:
            return self._calls_made

    @property
    def remaining_calls(self) -> int:
        return self.max_calls - self.calls_made

    def chat(
        self,
        messages: list[ChatMessage],
        trace: Any | None = None,
    ) -> ChatResponse:
        with self._lock:
            if self._calls_made >= self.max_calls:
                raise LLMBudgetExceeded(f"LLM call budget exhausted after {self.max_calls} calls")
            self._calls_made += 1
        return self.delegate.chat(messages, trace=trace)
