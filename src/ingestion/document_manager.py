"""Cross-store document lifecycle management for the local knowledge base."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ingestion.storage import LifecycleLock, StoredImage
from libs.loader import IngestionRecord


class VectorManagementStore(Protocol):
    """Minimal vector lifecycle contract required by management views."""

    def get_by_metadata(
        self,
        filters: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def delete_by_metadata(self, filters: dict[str, Any]) -> int: ...


class SparseManagementStore(Protocol):
    """Sparse lifecycle contract including legacy-ID fallback."""

    def count(
        self,
        *,
        collection: str | None = None,
        chunk_ids: list[str] | None = None,
    ) -> int: ...

    def remove_document(
        self,
        chunk_ids: list[str] | None = None,
        *,
        metadata_filters: Mapping[str, Any] | None = None,
    ) -> int: ...


class ImageManagementStore(Protocol):
    def list_images(
        self,
        *,
        collection: str | None = None,
        doc_hash: str | None = None,
    ) -> list[StoredImage]: ...

    def remove_document(self, doc_hash: str, *, collection: str | None = None) -> int: ...


class IntegrityManagementStore(Protocol):
    def list_processed(
        self,
        *,
        status: str | None = "success",
        collection: str | None = None,
    ) -> list[IngestionRecord]: ...

    def remove_record(
        self,
        *,
        file_hash: str | None = None,
        file_path: str | None = None,
        collection: str | None = None,
    ) -> int: ...


class ArtifactManagementStore(Protocol):
    def remove_files(self, paths: list[str]) -> int: ...


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    doc_id: str
    source_path: str
    collection: str
    chunk_count: int
    image_count: int
    ingested_at: str | None
    file_hash: str | None
    title: str | None


@dataclass(frozen=True, slots=True)
class DocumentDetail:
    document: DocumentInfo
    chunks: tuple[dict[str, Any], ...]
    images: tuple[StoredImage, ...]


@dataclass(frozen=True, slots=True)
class DeleteResult:
    source_path: str
    collection: str
    dense_deleted: int
    sparse_deleted: int
    images_deleted: int
    history_deleted: int
    artifacts_deleted: int = 0
    errors: tuple[str, ...] = ()

    @property
    def success(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class CollectionStats:
    collection: str | None
    document_count: int
    chunk_count: int
    sparse_chunk_count: int
    image_count: int


class DocumentManager:
    """Coordinate document reads and explicit deletion across all local stores."""

    def __init__(
        self,
        chroma_store: VectorManagementStore,
        bm25_indexer: SparseManagementStore,
        image_storage: ImageManagementStore,
        file_integrity: IntegrityManagementStore,
        artifact_storage: ArtifactManagementStore | None = None,
        lifecycle_lock: LifecycleLock | None = None,
    ) -> None:
        self.chroma_store = chroma_store
        self.bm25_indexer = bm25_indexer
        self.image_storage = image_storage
        self.file_integrity = file_integrity
        self.artifact_storage = artifact_storage
        database_path = getattr(file_integrity, "database_path", None)
        read_only = bool(getattr(file_integrity, "read_only", False))
        self.lifecycle_lock = lifecycle_lock or (
            LifecycleLock.for_database(database_path)
            if database_path is not None and not read_only
            else None
        )

    def list_documents(self, collection: str | None = None) -> list[DocumentInfo]:
        filters = {"collection": collection} if collection else None
        records = self.chroma_store.get_by_metadata(filters)
        history = self.file_integrity.list_processed(status="success", collection=collection)
        history_by_key = self._history_lookup(history)
        images_by_key = self._images_by_key(self.image_storage.list_images(collection=collection))

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in records:
            metadata = record.get("metadata", {})
            source_path = str(metadata.get("source_path") or "").strip()
            if not source_path:
                continue
            record_collection = str(metadata.get("collection") or "default")
            grouped.setdefault((source_path, record_collection), []).append(record)

        documents = [
            self._document_info(
                source_path, record_collection, chunks, history_by_key, images_by_key
            )
            for (source_path, record_collection), chunks in grouped.items()
        ]
        return sorted(
            documents, key=lambda item: (item.collection.lower(), item.source_path.lower())
        )

    def get_document_detail(self, doc_id: str) -> DocumentDetail:
        if not doc_id.strip():
            raise ValueError("doc_id must not be empty")
        try:
            document = next(item for item in self.list_documents() if item.doc_id == doc_id)
        except StopIteration as exc:
            raise KeyError(f"Unknown document: {doc_id}") from exc
        chunks = self.chroma_store.get_by_metadata(
            {"source_path": document.source_path, "collection": document.collection}
        )
        images = (
            self.image_storage.list_images(
                collection=document.collection,
                doc_hash=document.file_hash,
            )
            if document.file_hash
            else []
        )
        return DocumentDetail(document, tuple(chunks), tuple(images))

    def delete_document(self, source_path: str, collection: str) -> DeleteResult:
        """Delete an exact document target while reporting any partial-store failures."""
        if not source_path.strip() or not collection.strip():
            raise ValueError("source_path and collection must not be empty")
        if self.lifecycle_lock is not None:
            with self.lifecycle_lock.lease():
                return self._delete_document_unlocked(source_path, collection)
        return self._delete_document_unlocked(source_path, collection)

    def _delete_document_unlocked(self, source_path: str, collection: str) -> DeleteResult:
        refresh_generation = getattr(self.chroma_store, "refresh_active_generation", None)
        if callable(refresh_generation):
            # A preconstructed manager may outlive an ingestion collection swap. Never report a
            # canonical deletion while mutating the retained backup generation.
            refresh_generation()
        filters = {"source_path": source_path, "collection": collection}
        errors: list[str] = []
        records: list[dict[str, Any]] = []
        try:
            records = self.chroma_store.get_by_metadata(filters)
        except Exception as exc:  # pragma: no cover - exercised through failure doubles
            errors.append(self._error("vector lookup", exc))
        chunk_ids = [str(record["id"]) for record in records]
        vector_file_hashes = {
            str(record["metadata"].get("file_hash"))
            for record in records
            if record.get("metadata", {}).get("file_hash")
        }
        file_hashes = set(vector_file_hashes)
        try:
            history = self.file_integrity.list_processed(status=None, collection=None)
        except Exception as exc:  # pragma: no cover - exercised through failure doubles
            errors.append(self._error("history lookup", exc))
            history = []
        matching_history = [
            record
            for record in history
            if self._record_collection(record) == collection
            and (self._same_source(record, source_path) or record.file_hash in vector_file_hashes)
        ]
        file_hashes.update(record.file_hash for record in matching_history)

        sparse_deleted = self._attempt(
            errors,
            "BM25 deletion",
            lambda: self.bm25_indexer.remove_document(
                chunk_ids,
                metadata_filters=filters,
            ),
        )
        dense_deleted = self._attempt(
            errors,
            "vector deletion",
            lambda: self.chroma_store.delete_by_metadata(filters),
        )
        images_deleted = 0
        for file_hash in sorted(file_hashes):
            images_deleted += self._attempt(
                errors,
                "image deletion",
                lambda value=file_hash: self.image_storage.remove_document(
                    value, collection=collection
                ),
            )

        retained_records = [record for record in history if record not in matching_history]
        retained_artifact_keys = {
            self._artifact_key(path) for path in self._artifact_paths(retained_records)
        }
        artifact_paths = [
            path
            for path in self._artifact_paths(matching_history)
            if self._artifact_key(path) not in retained_artifact_keys
        ]
        artifacts_deleted = 0
        artifact_deletion_failed = False
        if artifact_paths:
            if self.artifact_storage is None:
                errors.append("artifact deletion: RuntimeError: artifact storage is not configured")
                artifact_deletion_failed = True
            else:
                try:
                    artifacts_deleted = self.artifact_storage.remove_files(artifact_paths)
                except Exception as exc:  # pragma: no cover - failure path covered by doubles
                    errors.append(self._error("artifact deletion", exc))
                    artifact_deletion_failed = True

        history_deleted = 0
        removed_history_hashes: set[str] = set()
        if not artifact_deletion_failed:
            for record in matching_history:
                if record.file_hash in removed_history_hashes:
                    continue
                history_deleted += self._attempt(
                    errors,
                    "history deletion",
                    lambda value=record.file_hash: self.file_integrity.remove_record(
                        file_hash=value,
                        collection=collection,
                    ),
                )
                removed_history_hashes.add(record.file_hash)
            if not matching_history:
                history_deleted = self._attempt(
                    errors,
                    "history deletion",
                    lambda: self.file_integrity.remove_record(
                        file_path=source_path,
                        collection=collection,
                    ),
                )
        return DeleteResult(
            source_path=source_path,
            collection=collection,
            dense_deleted=dense_deleted,
            sparse_deleted=sparse_deleted,
            images_deleted=images_deleted,
            history_deleted=history_deleted,
            artifacts_deleted=artifacts_deleted,
            errors=tuple(errors),
        )

    def get_collection_stats(self, collection: str | None = None) -> CollectionStats:
        documents = self.list_documents(collection)
        filters = {"collection": collection} if collection else None
        chunk_ids = [str(record["id"]) for record in self.chroma_store.get_by_metadata(filters)]
        return CollectionStats(
            collection=collection,
            document_count=len(documents),
            chunk_count=sum(item.chunk_count for item in documents),
            sparse_chunk_count=self.bm25_indexer.count(
                collection=collection,
                chunk_ids=chunk_ids if collection else None,
            ),
            image_count=sum(item.image_count for item in documents),
        )

    def _document_info(
        self,
        source_path: str,
        collection: str,
        chunks: list[dict[str, Any]],
        history_by_key: dict[tuple[str, str], IngestionRecord],
        images_by_key: dict[tuple[str, str], int],
    ) -> DocumentInfo:
        metadata = chunks[0].get("metadata", {})
        file_hash = str(metadata.get("file_hash") or "").strip() or None
        history = history_by_key.get(
            (collection, self._source_key(source_path))
        ) or history_by_key.get(
            (
                collection,
                self._source_key(str(metadata.get("source_relative_path") or "")),
            )
        )
        if history is not None:
            file_hash = file_hash or history.file_hash
        identity = file_hash or self._source_key(source_path)
        doc_id = hashlib.sha256(f"{collection}\0{identity}".encode()).hexdigest()
        title = str(metadata.get("title") or metadata.get("source_name") or "").strip() or None
        return DocumentInfo(
            doc_id=doc_id,
            source_path=source_path,
            collection=collection,
            chunk_count=len(chunks),
            image_count=images_by_key.get((collection, file_hash or ""), 0),
            ingested_at=history.processed_at if history else None,
            file_hash=file_hash,
            title=title,
        )

    @staticmethod
    def _history_lookup(
        records: list[IngestionRecord],
    ) -> dict[tuple[str, str], IngestionRecord]:
        lookup: dict[tuple[str, str], IngestionRecord] = {}
        for record in records:
            collection = DocumentManager._record_collection(record)
            lookup.setdefault((collection, DocumentManager._source_key(record.file_path)), record)
            source_relative = record.metadata.get("source_relative_path")
            if isinstance(source_relative, str):
                lookup.setdefault(
                    (collection, DocumentManager._source_key(source_relative)), record
                )
        return lookup

    @staticmethod
    def _images_by_key(images: list[StoredImage]) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for image in images:
            if image.doc_hash:
                key = (image.collection, image.doc_hash)
                counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _record_collection(record: IngestionRecord) -> str:
        return record.collection or str(record.metadata.get("collection") or "default")

    @staticmethod
    def _artifact_paths(records: list[IngestionRecord]) -> list[str]:
        paths: list[str] = []
        for record in records:
            for key in ("staged_pdf_path", "document_artifact_path"):
                value = record.metadata.get(key)
                if isinstance(value, str) and value.strip():
                    paths.append(value)
            chunk_paths = record.metadata.get("chunk_artifact_paths")
            if isinstance(chunk_paths, list):
                paths.extend(
                    value for value in chunk_paths if isinstance(value, str) and value.strip()
                )
            image_paths = record.metadata.get("extracted_image_artifact_paths")
            if isinstance(image_paths, list):
                paths.extend(
                    value for value in image_paths if isinstance(value, str) and value.strip()
                )
        return list(dict.fromkeys(paths))

    @staticmethod
    def _artifact_key(path: str) -> str:
        return str(Path(path).resolve()).replace("\\", "/").casefold()

    @staticmethod
    def _same_source(record: IngestionRecord, source_path: str) -> bool:
        source_key = DocumentManager._source_key(source_path)
        candidates = [record.file_path]
        for key in ("source_path", "source_relative_path", "staged_pdf_path"):
            value = record.metadata.get(key)
            if isinstance(value, str):
                candidates.append(value)
        return any(DocumentManager._source_key(candidate) == source_key for candidate in candidates)

    @staticmethod
    def _source_key(source_path: str) -> str:
        return source_path.replace("\\", "/")

    @classmethod
    def _attempt(cls, errors: list[str], stage: str, operation: Callable[[], int]) -> int:
        try:
            return int(operation())
        except Exception as exc:  # pragma: no cover - exercised through failure doubles
            errors.append(cls._error(stage, exc))
            return 0

    @staticmethod
    def _error(stage: str, exc: Exception) -> str:
        return f"{stage}: {type(exc).__name__}: {exc}"
