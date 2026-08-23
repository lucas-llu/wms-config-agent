"""Idempotent vector-store upsert adapter."""

from __future__ import annotations

from core.types import ChunkRecord
from libs.vector_store import BaseVectorStore


class VectorUpserter:
    def __init__(self, vector_store: BaseVectorStore, batch_size: int = 256) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")
        self.vector_store = vector_store
        self.batch_size = batch_size

    def upsert(self, records: list[ChunkRecord]) -> None:
        for start in range(0, len(records), self.batch_size):
            self.vector_store.upsert(records[start : start + self.batch_size])
