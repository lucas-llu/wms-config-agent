"""Generate stable, source-first citations from retrieval results."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from core.types import RetrievalResult


@dataclass(frozen=True, slots=True)
class Citation:
    index: int
    chunk_id: str
    title: str
    source: str
    page_start: int | None
    page_end: int | None
    process_code: str | None
    document_type: str | None
    score: float
    excerpt: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def page_label(self) -> str:
        if self.page_start is None:
            return "页码未知"
        if self.page_end is None or self.page_end == self.page_start:
            return f"第 {self.page_start} 页"
        return f"第 {self.page_start}-{self.page_end} 页"


class CitationGenerator:
    def __init__(self, excerpt_length: int = 500) -> None:
        if excerpt_length <= 0:
            raise ValueError("excerpt_length must be greater than 0")
        self.excerpt_length = excerpt_length

    def generate(self, results: list[RetrievalResult]) -> list[Citation]:
        return [
            Citation(
                index=index,
                chunk_id=result.chunk_id,
                title=str(result.metadata.get("title") or "Untitled document"),
                source=str(
                    result.metadata.get("source_relative_path")
                    or result.metadata.get("source_path")
                ),
                page_start=self._optional_int(result.metadata.get("page_start")),
                page_end=self._optional_int(result.metadata.get("page_end")),
                process_code=self._optional_str(result.metadata.get("process_code")),
                document_type=self._optional_str(
                    result.metadata.get("document_type")
                ),
                score=result.score,
                excerpt=self._excerpt(result.text),
                metadata=dict(result.metadata),
            )
            for index, result in enumerate(results, start=1)
        ]

    def _excerpt(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        if len(normalized) <= self.excerpt_length:
            return normalized
        return normalized[: self.excerpt_length].rstrip() + "…"

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return int(value) if isinstance(value, int | float) else None

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        return str(value) if value is not None else None
