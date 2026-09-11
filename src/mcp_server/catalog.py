"""Read-only catalog over private preprocessed chunk artifacts."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from agents.workspace import Workspace
from core.types import Chunk
from ingestion import load_preprocessed_chunks
from libs.loader import SQLiteIntegrityChecker

_MISSING_SCOPE_VALUES = frozenset({"", "n/a", "none", "unknown", "unspecified"})


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    document_id: str
    title: str
    source: str
    collection: str
    domain: str | None
    process_code: str | None
    process_stage: str | None
    document_type: str | None
    page_count: int | None
    chunk_count: int
    excerpt: str
    file_hash: str | None
    version: str | None
    module: str | None
    site: str | None
    environment: str | None
    scope_missing_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_catalog_dict(self, freshness: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "source": self.source,
            "collection": self.collection,
            "domain": self.domain,
            "process_code": self.process_code,
            "process_stage": self.process_stage,
            "document_type": self.document_type,
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "scope": {
                "version": self.version,
                "module": self.module,
                "site": self.site,
                "environment": self.environment,
                "status": "complete" if not self.scope_missing_fields else "incomplete",
                "missing_fields": list(self.scope_missing_fields),
            },
            "freshness": freshness,
        }


class CorpusCatalog:
    def __init__(
        self,
        chunks_path: str | Path,
        workspace: Workspace | None = None,
        *,
        dense_index: Any | None = None,
        sparse_index: Any | None = None,
        history_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.chunks_path = Path(chunks_path)
        self.workspace = workspace
        self.dense_index = dense_index
        self.sparse_index = sparse_index
        self.history_path = Path(history_path) if history_path is not None else None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._chunks: list[Chunk] | None = None
        self._documents: list[DocumentSummary] | None = None

    def list_collections(self) -> list[dict[str, Any]]:
        chunks = self._load_chunks()
        documents = self._load_documents()
        document_counts: dict[str, int] = defaultdict(int)
        domains: dict[str, set[str]] = defaultdict(set)
        for document in documents:
            document_counts[document.collection] += 1
            if document.domain:
                domains[document.collection].add(document.domain)
        chunk_counts: dict[str, int] = defaultdict(int)
        for chunk in chunks:
            chunk_counts[str(chunk.metadata.get("collection", "default"))] += 1
        return [
            {
                "name": collection,
                "document_count": document_counts[collection],
                "chunk_count": chunk_counts[collection],
                "domains": sorted(domains[collection]),
            }
            for collection in sorted(set(document_counts) | set(chunk_counts))
        ]

    def find_documents(self, identifier: str) -> list[DocumentSummary]:
        normalized = identifier.strip().lower()
        if not normalized:
            raise ValueError("document_id must be a non-empty string")
        return [
            document
            for document in self._load_documents()
            if normalized
            in {
                document.document_id.lower(),
                document.source.lower(),
                (document.process_code or "").lower(),
            }
        ]

    def knowledge_snapshot(
        self,
        *,
        collection: str | None = None,
        module: str | None = None,
        scope_status: str | None = None,
        freshness_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise ValueError("limit must be greater than 0")
        if offset < 0:
            raise ValueError("offset must be zero or greater")
        history = self._load_history()
        documents = [
            document
            for document in self._load_documents()
            if _matches_filters(
                document,
                history,
                collection=collection,
                module=module,
                scope_status=scope_status,
                freshness_status=freshness_status,
            )
        ]
        records = [
            (
                document,
                _freshness(document.file_hash, history),
            )
            for document in documents
        ]
        page = records[offset : offset + limit]
        return {
            "catalog_version": 1,
            "workspace": self._workspace_summary(),
            "summary": _catalog_summary(records),
            "index_health": self._index_health(len(self._load_chunks())),
            "documents": [document.to_catalog_dict(freshness) for document, freshness in page],
            "pagination": {
                "offset": offset,
                "limit": limit,
                "returned": len(page),
                "total": len(records),
            },
        }

    def _load_chunks(self) -> list[Chunk]:
        if self._chunks is None:
            chunks = load_preprocessed_chunks(self.chunks_path)
            if self.workspace is not None:
                chunks = [
                    chunk for chunk in chunks if self.workspace.permits_metadata(chunk.metadata)
                ]
            self._chunks = chunks
        return self._chunks

    def _load_documents(self) -> list[DocumentSummary]:
        if self._documents is not None:
            return self._documents
        grouped: dict[str, list[Chunk]] = defaultdict(list)
        for chunk in self._load_chunks():
            metadata = chunk.metadata
            file_hash = _optional_text(metadata.get("file_hash"))
            source = _safe_source(metadata)
            document_id = f"doc-{file_hash[:16]}" if file_hash else source
            grouped[document_id].append(chunk)

        documents: list[DocumentSummary] = []
        for document_id, chunks in grouped.items():
            ordered = sorted(chunks, key=lambda chunk: chunk.start_offset)
            metadata = ordered[0].metadata
            excerpt = re.sub(r"\s+", " ", ordered[0].text).strip()[:500]
            version = _scope_value(metadata.get("version"))
            module = _scope_value(metadata.get("module"))
            site = _scope_value(metadata.get("site"))
            environment = _scope_value(metadata.get("environment"))
            missing_fields = tuple(
                field
                for field, value in (
                    ("version", version),
                    ("module", module),
                    ("site", site),
                    ("environment", environment),
                )
                if value is None
            )
            documents.append(
                DocumentSummary(
                    document_id=document_id,
                    title=str(metadata.get("title") or metadata.get("source_name") or document_id),
                    source=_safe_source(metadata),
                    collection=str(metadata.get("collection", "default")),
                    domain=self._optional_str(metadata.get("domain")),
                    process_code=self._optional_str(metadata.get("process_code")),
                    process_stage=self._optional_str(metadata.get("process_stage")),
                    document_type=self._optional_str(metadata.get("document_type")),
                    page_count=self._optional_int(metadata.get("page_count")),
                    chunk_count=len(ordered),
                    excerpt=excerpt,
                    file_hash=_optional_text(metadata.get("file_hash")),
                    version=version,
                    module=module,
                    site=site,
                    environment=environment,
                    scope_missing_fields=missing_fields,
                )
            )
        self._documents = sorted(
            documents, key=lambda document: (document.source, document.document_id)
        )
        return self._documents

    def _load_history(self) -> dict[str, Any]:
        if self.history_path is None or not self.history_path.is_file():
            return {}
        try:
            records = SQLiteIntegrityChecker(self.history_path, read_only=True).list_processed()
        except (OSError, RuntimeError, TimeoutError, sqlite3.DatabaseError):
            return {}
        by_hash: dict[str, Any] = {}
        for record in records:
            by_hash.setdefault(record.file_hash, record)
        return by_hash

    def _index_health(self, processed_chunk_count: int) -> dict[str, Any]:
        scoped = self.workspace is not None and self.workspace.workspace_id != "workspace:legacy"
        dense_count = None if scoped else _count_index(self.dense_index)
        sparse_count = None if scoped else _count_index(self.sparse_index)
        aligned = (
            dense_count == sparse_count == processed_chunk_count
            if dense_count is not None and sparse_count is not None
            else None
        )
        if scoped:
            status = "unverified"
        elif dense_count is None or sparse_count is None:
            status = "unavailable"
        elif aligned:
            status = "healthy"
        else:
            status = "degraded"
        return {
            "status": status,
            "scope": "workspace" if scoped else "shared_index",
            "dense_count": dense_count,
            "sparse_count": sparse_count,
            "processed_chunk_count": processed_chunk_count,
            "aligned": aligned,
            "checked_at": _timestamp(self._clock()),
        }

    def _workspace_summary(self) -> dict[str, Any]:
        if self.workspace is None:
            return {
                "workspace_id": "workspace:legacy",
                "scope_enforced": False,
                "policy_mode": "unrestricted",
            }
        return {
            "workspace_id": self.workspace.workspace_id,
            "scope_enforced": self.workspace.workspace_id != "workspace:legacy",
            "policy_mode": "immutable_allowlist",
        }

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return int(value) if isinstance(value, int | float) else None


def _matches_filters(
    document: DocumentSummary,
    history: dict[str, Any],
    *,
    collection: str | None,
    module: str | None,
    scope_status: str | None,
    freshness_status: str | None,
) -> bool:
    if collection is not None and document.collection.casefold() != collection.casefold():
        return False
    if module is not None and (document.module or "").casefold() != module.casefold():
        return False
    document_scope_status = "complete" if not document.scope_missing_fields else "incomplete"
    if scope_status is not None and document_scope_status != scope_status:
        return False
    if freshness_status is not None:
        current = _freshness(document.file_hash, history)["status"]
        if current != freshness_status:
            return False
    return True


def _catalog_summary(records: list[tuple[DocumentSummary, dict[str, Any]]]) -> dict[str, int]:
    freshness_counts = defaultdict(int)
    for _, freshness in records:
        freshness_counts[str(freshness["status"])] += 1
    incomplete = sum(bool(document.scope_missing_fields) for document, _ in records)
    return {
        "document_count": len(records),
        "collection_count": len({document.collection for document, _ in records}),
        "scope_complete_count": len(records) - incomplete,
        "scope_incomplete_count": incomplete,
        "fresh_count": freshness_counts["fresh"],
        "stale_count": freshness_counts["stale"],
        "unknown_freshness_count": freshness_counts["unknown"],
    }


def _freshness(file_hash: str | None, history: dict[str, Any]) -> dict[str, Any]:
    record = history.get(file_hash) if file_hash else None
    if record is None:
        return {
            "status": "unknown",
            "processed_at": None,
            "reason": "not_recorded",
        }
    processed_at = _parse_timestamp(str(record.processed_at))
    if processed_at is None:
        return {
            "status": "unknown",
            "processed_at": str(record.processed_at),
            "reason": "invalid_timestamp",
        }
    try:
        source_mtime = datetime.fromtimestamp(Path(str(record.file_path)).stat().st_mtime, tz=UTC)
    except OSError:
        return {
            "status": "unknown",
            "processed_at": str(record.processed_at),
            "reason": "source_unavailable",
        }
    if source_mtime > processed_at:
        status = "stale"
        reason = "source_newer_than_index"
    else:
        status = "fresh"
        reason = "source_current"
    return {
        "status": status,
        "processed_at": str(record.processed_at),
        "reason": reason,
    }


def _count_index(index: Any | None) -> int | None:
    if index is None:
        return None
    try:
        return int(index.count())
    except Exception:
        return None


def _safe_source(metadata: dict[str, Any]) -> str:
    for key in ("source_relative_path", "source_name", "source_path"):
        value = _optional_text(metadata.get(key))
        if value:
            return _safe_reference(value)
    return "unknown"


def _safe_reference(value: str) -> str:
    normalized = value.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.drive or windows.root:
        return posix.name or "unknown"
    if any(part == ".." for part in posix.parts):
        return posix.name or "unknown"
    return posix.as_posix()


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _scope_value(value: Any) -> str | None:
    normalized = _optional_text(value)
    if normalized is None or normalized.casefold() in _MISSING_SCOPE_VALUES:
        return None
    return normalized


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
