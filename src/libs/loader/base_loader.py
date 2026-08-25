"""Document loader extension contract and shared metadata helpers."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.types import Document

DomainMetadata = Mapping[str, Any]


class BaseLoader(ABC):
    """Convert one authorized local file into the shared Document contract."""

    @abstractmethod
    def load(self, path: str | Path, metadata: DomainMetadata | None = None) -> Document:
        """Load a local file without performing network access."""

    @staticmethod
    def validate_path(path: str | Path, allowed_extensions: set[str]) -> Path:
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Document does not exist: {file_path}")
        suffix = file_path.suffix.lower()
        if suffix not in allowed_extensions:
            allowed = ", ".join(sorted(allowed_extensions))
            raise ValueError(f"Unsupported document type '{suffix}'; expected one of: {allowed}")
        return file_path

    @staticmethod
    def compute_file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def build_metadata(
        cls,
        path: Path,
        *,
        doc_type: str,
        metadata: DomainMetadata | None = None,
        file_hash: str | None = None,
    ) -> dict[str, Any]:
        """Build canonical source fields while preserving caller-supplied domain metadata."""
        values: dict[str, Any] = {
            "collection": "default",
            "version": "unspecified",
            "module": "general",
            "site": "unspecified",
            "environment": "unspecified",
        }
        if metadata:
            values.update(metadata)
        values.update(
            {
                "source_path": path.as_posix(),
                "source_name": path.name,
                "doc_type": doc_type,
                "title": values.get("title") or path.stem,
                "file_hash": file_hash or cls.compute_file_hash(path),
            }
        )
        return values

    @staticmethod
    def build_document_id(file_hash: str) -> str:
        return f"doc-{file_hash[:16]}"
