"""Persistent Okapi BM25 inverted index."""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ingestion.embedding import SparseEncoder, SparseEncoding
from ingestion.storage.lifecycle_lock import LifecycleLock
from libs.atomic_file import replace_file_atomically

_LOCK_TIMEOUT_SECONDS = 30.0


class BM25Indexer:
    """JSON-backed BM25 index with cross-instance refresh and atomic mutations."""

    def __init__(
        self,
        persist_path: str | Path = "data/db/bm25",
        *,
        k1: float = 1.5,
        b: float = 0.75,
        read_only: bool = False,
    ) -> None:
        self.persist_path = Path(persist_path)
        self.index_path = self.persist_path / "index.json"
        self.lock_path = self.persist_path / ".index.lock"
        self.k1 = k1
        self.b = b
        self.read_only = read_only
        self.documents: dict[str, dict[str, Any]] = {}
        self.postings: dict[str, dict[str, int]] = {}
        self.average_document_length = 0.0
        self._disk_signature: tuple[int, int, int, int] | None = None
        if self.index_path.is_file():
            self.load()

    def build(self, encodings: list[SparseEncoding]) -> None:
        """Replace the complete index with a caller-provided corpus."""
        self._ensure_writable()
        with self._exclusive_lock():
            self.documents = {
                encoding.chunk_id: self._document_payload(encoding) for encoding in encodings
            }
            self._rebuild_postings()
            self._save_unlocked()

    def upsert(self, encodings: list[SparseEncoding]) -> None:
        self._ensure_writable()
        if not encodings:
            return
        with self._exclusive_lock():
            self._load_latest_unlocked()
            for encoding in encodings:
                self.documents[encoding.chunk_id] = self._document_payload(encoding)
            self._rebuild_postings()
            self._save_unlocked()

    def query(self, query: str | list[str], top_k: int = 10) -> list[dict[str, Any]]:
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0")
        self._refresh_if_changed()
        terms = SparseEncoder.tokenize(query) if isinstance(query, str) else query
        if not terms or not self.documents:
            return []
        scores: dict[str, float] = {}
        document_count = len(self.documents)
        for term in (value.lower() for value in terms):
            term_postings = self.postings.get(term)
            if not term_postings:
                continue
            document_frequency = len(term_postings)
            idf = math.log(
                1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for chunk_id, frequency in term_postings.items():
                document_length = int(self.documents[chunk_id]["length"])
                denominator = frequency + self.k1 * (
                    1.0 - self.b + self.b * document_length / max(self.average_document_length, 1.0)
                )
                score = idf * (frequency * (self.k1 + 1.0)) / denominator
                scores[chunk_id] = scores.get(chunk_id, 0.0) + score
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return [{"chunk_id": chunk_id, "score": score} for chunk_id, score in ranked]

    def save(self) -> None:
        """Persist the in-memory corpus atomically.

        Lifecycle mutations should use :meth:`build`, :meth:`upsert`, or
        :meth:`remove_document`, which coordinate against the latest disk version.
        """
        self._ensure_writable()
        with self._exclusive_lock():
            self._save_unlocked()

    def load(self) -> None:
        self._load_latest_unlocked()

    def count(
        self,
        *,
        collection: str | None = None,
        chunk_ids: list[str] | None = None,
    ) -> int:
        """Count all chunks or the union of collection metadata and supplied legacy IDs."""
        self._refresh_if_changed()
        if collection is None and chunk_ids is None:
            return len(self.documents)
        selected_ids = set(chunk_ids or [])
        return sum(
            1
            for chunk_id, document in self.documents.items()
            if chunk_id in selected_ids
            or (
                collection is not None
                and self._metadata_matches(document, {"collection": collection})
            )
        )

    def remove_document(
        self,
        chunk_ids: list[str] | None = None,
        *,
        metadata_filters: Mapping[str, Any] | None = None,
    ) -> int:
        """Remove IDs and/or metadata matches from the latest persisted index.

        Metadata-backed removal allows sparse orphan cleanup even when the vector store is
        unavailable. Supplying vector IDs remains a compatibility fallback for schema-v1
        indexes that did not persist management metadata.
        """
        self._ensure_writable()
        if not chunk_ids and not metadata_filters:
            return 0
        target_ids = set(chunk_ids or [])
        with self._exclusive_lock():
            self._load_latest_unlocked()
            removed_ids = {
                chunk_id
                for chunk_id, document in self.documents.items()
                if chunk_id in target_ids
                or (
                    metadata_filters is not None
                    and self._metadata_matches(document, metadata_filters)
                )
            }
            for chunk_id in removed_ids:
                self.documents.pop(chunk_id, None)
            if removed_ids:
                self._rebuild_postings()
                self._save_unlocked()
            return len(removed_ids)

    @staticmethod
    def _document_payload(encoding: SparseEncoding) -> dict[str, Any]:
        return {
            "length": encoding.document_length,
            "term_frequencies": encoding.term_frequencies,
            "metadata": dict(encoding.metadata),
        }

    @staticmethod
    def _metadata_matches(document: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
        metadata = document.get("metadata")
        return isinstance(metadata, Mapping) and all(
            metadata.get(key) == value for key, value in filters.items()
        )

    def _refresh_if_changed(self) -> None:
        signature = self._file_signature()
        if signature != self._disk_signature:
            self._load_latest_unlocked()

    def _load_latest_unlocked(self) -> None:
        if not self.index_path.is_file():
            self.documents = {}
            self.postings = {}
            self.average_document_length = 0.0
            self._disk_signature = None
            return
        values: dict[str, Any] | None = None
        signature: tuple[int, int, int, int] | None = None
        for _ in range(8):
            before = self._file_signature()
            if before is None:
                continue
            payload = self.index_path.read_text(encoding="utf-8")
            after = self._file_signature()
            if before == after:
                values = json.loads(payload)
                signature = after
                break
        if values is None or signature is None:
            raise RuntimeError(f"BM25 index changed continuously while loading: {self.index_path}")
        self.k1 = float(values["k1"])
        self.b = float(values["b"])
        self.documents = values["documents"]
        # Postings can be rebuilt and this also tolerates old or partial derived data.
        self._rebuild_postings()
        self._disk_signature = signature

    def _save_unlocked(self) -> None:
        self.persist_path.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_name(f".{self.index_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as destination:
                json.dump(
                    {
                        "schema_version": 2,
                        "k1": self.k1,
                        "b": self.b,
                        "average_document_length": self.average_document_length,
                        "documents": self.documents,
                        "postings": self.postings,
                    },
                    destination,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                destination.flush()
                os.fsync(destination.fileno())
            replace_file_atomically(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)
        self._disk_signature = self._file_signature()

    def _file_signature(self) -> tuple[int, int, int, int] | None:
        try:
            stat = self.index_path.stat()
        except FileNotFoundError:
            return None
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise PermissionError("BM25Indexer is read-only")

    def _exclusive_lock(self) -> LifecycleLock:
        return LifecycleLock(
            self.lock_path,
            timeout_seconds=_LOCK_TIMEOUT_SECONDS,
            poll_interval_seconds=0.02,
        )

    def _rebuild_postings(self) -> None:
        self.postings = {}
        total_length = 0
        for chunk_id, document in self.documents.items():
            total_length += int(document["length"])
            for term, frequency in document["term_frequencies"].items():
                self.postings.setdefault(term, {})[chunk_id] = int(frequency)
        self.average_document_length = total_length / max(len(self.documents), 1)
