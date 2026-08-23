"""Stable data contracts shared by ingestion, retrieval, and MCP layers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

Metadata = dict[str, Any]


class ContractError(ValueError):
    """Raised when a core data contract is constructed with invalid data."""


@dataclass(slots=True)
class SerializableContract:
    """Provide deterministic dictionary and JSON serialization."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(slots=True)
class Document(SerializableContract):
    id: str
    text: str
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_identity(self.id, "Document.id")
        _validate_metadata(self.metadata)


@dataclass(slots=True)
class Chunk(SerializableContract):
    id: str
    text: str
    metadata: Metadata
    start_offset: int
    end_offset: int
    source_ref: str | None = None

    def __post_init__(self) -> None:
        _validate_identity(self.id, "Chunk.id")
        _validate_metadata(self.metadata)
        if self.start_offset < 0:
            raise ContractError("Chunk.start_offset must be greater than or equal to 0")
        if self.end_offset < self.start_offset:
            raise ContractError("Chunk.end_offset must be greater than or equal to start_offset")


@dataclass(slots=True)
class ChunkRecord(SerializableContract):
    id: str
    text: str
    metadata: Metadata
    dense_vector: list[float] | None = None
    sparse_vector: dict[str, float] | None = None

    def __post_init__(self) -> None:
        _validate_identity(self.id, "ChunkRecord.id")
        _validate_metadata(self.metadata)


def _validate_identity(value: str, field_path: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field_path} must be a non-empty string")


def _validate_metadata(metadata: Metadata) -> None:
    if not isinstance(metadata, dict):
        raise ContractError("metadata must be a dictionary")
    source_path = metadata.get("source_path")
    if not isinstance(source_path, str) or not source_path.strip():
        raise ContractError("metadata.source_path must be a non-empty string")
    images = metadata.get("images", [])
    if not isinstance(images, list):
        raise ContractError("metadata.images must be a list when provided")
