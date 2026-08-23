"""Stable batching for dense encoding."""

from __future__ import annotations

from collections.abc import Callable

from core.types import Chunk, ChunkRecord
from ingestion.embedding.dense_encoder import DenseEncoder

ProgressCallback = Callable[[int, int], None]


class BatchProcessor:
    def __init__(self, encoder: DenseEncoder, batch_size: int = 32) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")
        self.encoder = encoder
        self.batch_size = batch_size

    def encode(
        self,
        chunks: list[Chunk],
        *,
        on_progress: ProgressCallback | None = None,
    ) -> list[ChunkRecord]:
        records: list[ChunkRecord] = []
        total = len(chunks)
        for start in range(0, total, self.batch_size):
            records.extend(self.encoder.encode(chunks[start : start + self.batch_size]))
            if on_progress:
                on_progress(min(start + self.batch_size, total), total)
        return records
