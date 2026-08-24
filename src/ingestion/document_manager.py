"""Cross-store document lifecycle management for the local knowledge base."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ingestion.storage import BM25Indexer, ImageStorage, StoredImage
from libs.loader import IngestionRecord, SQLiteIntegrityChecker
from libs.vector_store import ChromaStore


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
        chroma_store: ChromaStore,
        bm25_indexer: BM25Indexer,
        image_storage: ImageStorage,
        file_integrity: SQLiteIntegrityChecker,
    ) -> None:
        self.chroma_store = chroma_store
        self.bm25_indexer = bm25_indexer
        self.image_storage = image_storage
        self.file_integrity = file_integrity

    def list_documents(self, collection: str | None = None) -> list[DocumentInfo]:
        filters = {"collection": collection} if collection else None
        records = self.chroma_store.get_by_metadata(filters)
        history = self.file_integrity.list_processed(status="success")
        history_by_key = self._history_lookup(history)
        images_by_hash = self._images_by_hash(self.image_storage.list_images(collection=collection))

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
                source_path, record_collection, chunks, history_by_key, images_by_hash
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
        filters = {"source_path": source_path, "collection": collection}
        errors: list[str] = []
        records: list[dict[str, Any]] = []
        try:
            records = self.chroma_store.get_by_metadata(filters)
        except Exception as exc:  # pragma: no cover - exercised through failure doubles
            errors.append(self._error("vector lookup", exc))
        chunk_ids = [str(record["id"]) for record in records]
        file_hashes = {
            str(record["metadata"].get("file_hash"))
            for record in records
            if record.get("metadata", {}).get("file_hash")
        }
        try:
            history = self.file_integrity.list_processed(status=None)
        except Exception as exc:  # pragma: no cover - exercised through failure doubles
            errors.append(self._error("history lookup", exc))
            history = []
        for record in history:
            if self._same_source(record, source_path):
                file_hashes.add(record.file_hash)

        sparse_deleted = self._attempt(
            errors,
            "BM25 deletion",
            lambda: self.bm25_indexer.remove_document(chunk_ids),
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
        history_deleted = self._attempt(
            errors,
            "history deletion",
            lambda: self.file_integrity.remove_record(file_path=source_path),
        )
        for record in history:
            if record.file_hash in file_hashes and record.file_path != source_path:
                history_deleted += self._attempt(
                    errors,
                    "history deletion",
                    lambda value=record.file_hash: self.file_integrity.remove_record(
                        file_hash=value
                    ),
                )
        return DeleteResult(
            source_path=source_path,
            collection=collection,
            dense_deleted=dense_deleted,
            sparse_deleted=sparse_deleted,
            images_deleted=images_deleted,
            history_deleted=history_deleted,
            errors=tuple(errors),
        )

    def get_collection_stats(self, collection: str | None = None) -> CollectionStats:
        documents = self.list_documents(collection)
        return CollectionStats(
            collection=collection,
            document_count=len(documents),
            chunk_count=sum(item.chunk_count for item in documents),
            sparse_chunk_count=self.bm25_indexer.count(),
            image_count=sum(item.image_count for item in documents),
        )

    def _document_info(
        self,
        source_path: str,
        collection: str,
        chunks: list[dict[str, Any]],
        history_by_key: dict[str, IngestionRecord],
        images_by_hash: dict[str, int],
    ) -> DocumentInfo:
        metadata = chunks[0].get("metadata", {})
        file_hash = str(metadata.get("file_hash") or "").strip() or None
        history = history_by_key.get(self._source_key(source_path)) or history_by_key.get(
            self._source_key(str(metadata.get("source_relative_path") or ""))
        )
        if history is not None:
            file_hash = file_hash or history.file_hash
        doc_id = file_hash or hashlib.sha256(f"{collection}\0{source_path}".encode()).hexdigest()
        title = str(metadata.get("title") or metadata.get("source_name") or "").strip() or None
        return DocumentInfo(
            doc_id=doc_id,
            source_path=source_path,
            collection=collection,
            chunk_count=len(chunks),
            image_count=images_by_hash.get(file_hash or "", 0),
            ingested_at=history.processed_at if history else None,
            file_hash=file_hash,
            title=title,
        )

    @staticmethod
    def _history_lookup(records: list[IngestionRecord]) -> dict[str, IngestionRecord]:
        lookup: dict[str, IngestionRecord] = {}
        for record in records:
            lookup.setdefault(DocumentManager._source_key(record.file_path), record)
            source_relative = record.metadata.get("source_relative_path")
            if isinstance(source_relative, str):
                lookup.setdefault(DocumentManager._source_key(source_relative), record)
        return lookup

    @staticmethod
    def _images_by_hash(images: list[StoredImage]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for image in images:
            if image.doc_hash:
                counts[image.doc_hash] = counts.get(image.doc_hash, 0) + 1
        return counts

    @staticmethod
    def _same_source(record: IngestionRecord, source_path: str) -> bool:
        if DocumentManager._source_key(record.file_path) == DocumentManager._source_key(
            source_path
        ):
            return True
        relative = record.metadata.get("source_relative_path")
        return isinstance(relative, str) and DocumentManager._source_key(
            relative
        ) == DocumentManager._source_key(source_path)

    @staticmethod
    def _source_key(source_path: str) -> str:
        return source_path.replace("\\", "/")

    @classmethod
    def _attempt(cls, errors: list[str], stage: str, operation: Any) -> int:
        try:
            return int(operation())
        except Exception as exc:  # pragma: no cover - exercised through failure doubles
            errors.append(cls._error(stage, exc))
            return 0

    @staticmethod
    def _error(stage: str, exc: Exception) -> str:
        return f"{stage}: {type(exc).__name__}: {exc}"
