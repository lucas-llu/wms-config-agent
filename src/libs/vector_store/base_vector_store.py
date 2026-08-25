"""Vector store extension contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from core.types import ChunkRecord


class BaseVectorStore(ABC):
    @abstractmethod
    def upsert(self, records: list[ChunkRecord], trace: Any | None = None) -> None:
        """Insert or update records by stable chunk ID."""

    @abstractmethod
    def query(
        self,
        vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
        trace: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Return nearest records ordered by descending similarity."""

    @abstractmethod
    def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Fetch records while preserving the requested ID order."""

    @abstractmethod
    def count(self) -> int:
        """Return the number of stored records."""

    def list_ids(self) -> list[str]:
        """Return all stored IDs when the backend supports corpus synchronization."""
        raise NotImplementedError

    def delete(self, ids: list[str]) -> None:
        """Delete records by ID when the backend supports corpus synchronization."""
        raise NotImplementedError
