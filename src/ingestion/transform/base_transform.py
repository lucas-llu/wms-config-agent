"""Atomic transform contract for ingestion-time chunk enrichment."""

from __future__ import annotations

import copy
import time
from abc import ABC, abstractmethod
from typing import Any

from core.types import Chunk


class BaseTransform(ABC):
    """Transform chunks without mutating caller-owned contracts."""

    name = "transform"

    @abstractmethod
    def transform(self, chunks: list[Chunk], trace: Any | None = None) -> list[Chunk]:
        """Return transformed chunks in the same order as the input."""

    @staticmethod
    def clone_chunk(
        chunk: Chunk,
        *,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Chunk:
        return Chunk(
            id=chunk.id,
            text=chunk.text if text is None else text,
            metadata=copy.deepcopy(chunk.metadata) if metadata is None else metadata,
            start_offset=chunk.start_offset,
            end_offset=chunk.end_offset,
            source_ref=chunk.source_ref,
        )

    @staticmethod
    def record_trace(
        trace: Any | None,
        *,
        name: str,
        started: float,
        details: dict[str, Any],
    ) -> None:
        if trace is not None and hasattr(trace, "record_stage"):
            trace.record_stage(
                f"transform.{name}",
                (time.perf_counter() - started) * 1000,
                details=details,
            )
