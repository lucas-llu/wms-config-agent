"""Provider-neutral text LLM contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

ChatMessage = dict[str, str]


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Normalized provider response consumed by ingestion transforms."""

    content: str
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseLLM(ABC):
    """Text generation interface shared by all LLM providers."""

    @abstractmethod
    def chat(
        self,
        messages: list[ChatMessage],
        trace: Any | None = None,
    ) -> ChatResponse:
        """Generate one normalized response for a chat message sequence."""
