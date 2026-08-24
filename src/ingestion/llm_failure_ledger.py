"""Persistent chunk-level ledger for recoverable ingestion LLM fallbacks."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from core.types import Chunk

_RETRYABLE_IMAGE_STATUSES = frozenset({"failed", "partial", "vision_llm_unavailable"})


@dataclass(frozen=True, slots=True)
class LLMFallback:
    """One transform fallback that can be retried without reloading a document."""

    document_id: str
    source_path: str
    chunk_id: str
    transform: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def collect_llm_fallbacks(
    chunks: list[Chunk],
    *,
    document_id: str,
    source_path: str,
) -> tuple[LLMFallback, ...]:
    """Collect only failures from active LLM paths, excluding expected rule-only states."""

    failures: list[LLMFallback] = []
    for chunk in chunks:
        metadata = chunk.metadata
        refinement_reason = metadata.get("refinement_fallback_reason")
        if (
            metadata.get("refinement_llm_enabled") is True
            and isinstance(refinement_reason, str)
            and refinement_reason != "empty_rule_result"
        ):
            failures.append(
                LLMFallback(
                    document_id=document_id,
                    source_path=source_path,
                    chunk_id=chunk.id,
                    transform="chunk_refiner",
                    reason=refinement_reason,
                )
            )

        enrichment_reason = metadata.get("metadata_enrichment_fallback_reason")
        if isinstance(enrichment_reason, str):
            failures.append(
                LLMFallback(
                    document_id=document_id,
                    source_path=source_path,
                    chunk_id=chunk.id,
                    transform="metadata_enricher",
                    reason=enrichment_reason,
                )
            )

        image_status = metadata.get("image_caption_status")
        if isinstance(image_status, str) and image_status in _RETRYABLE_IMAGE_STATUSES:
            failures.append(
                LLMFallback(
                    document_id=document_id,
                    source_path=source_path,
                    chunk_id=chunk.id,
                    transform="image_captioner",
                    reason=image_status,
                )
            )

    return tuple(failures)


class LLMFailureLedger:
    """Maintain a deterministic, atomically written JSONL fallback ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries = self._load()

    @property
    def entries(self) -> tuple[LLMFallback, ...]:
        return tuple(self._entries)

    @property
    def count(self) -> int:
        return len(self._entries)

    def update_document(
        self,
        document_id: str,
        failures: tuple[LLMFallback, ...],
    ) -> None:
        self._entries = [item for item in self._entries if item.document_id != document_id]
        self._entries.extend(failures)
        self._entries.sort(key=lambda item: (item.source_path, item.chunk_id, item.transform))

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        content = "".join(
            json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            for item in self._entries
        )
        temporary.write_text(content, encoding="utf-8")
        for attempt in range(6):
            try:
                temporary.replace(self.path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (2**attempt))

    def _load(self) -> list[LLMFallback]:
        if not self.path.is_file():
            return []
        entries: list[LLMFallback] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                entries.append(LLMFallback(**payload))
            except (json.JSONDecodeError, TypeError):
                continue
        return entries
