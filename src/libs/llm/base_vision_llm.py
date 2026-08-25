"""Provider-neutral Vision LLM contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from libs.llm.base_llm import ChatResponse


class BaseVisionLLM(ABC):
    """Multimodal interface for text plus a local path or encoded image bytes."""

    @abstractmethod
    def chat_with_image(
        self,
        text: str,
        image_path: str | Path | bytes,
        trace: Any | None = None,
    ) -> ChatResponse:
        """Describe an image using the supplied textual context."""
