"""Text splitter extension contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseSplitter(ABC):
    """Split text without depending on ingestion domain objects."""

    @abstractmethod
    def split_text(self, text: str, trace: Any | None = None) -> list[str]:
        """Return ordered text fragments."""
