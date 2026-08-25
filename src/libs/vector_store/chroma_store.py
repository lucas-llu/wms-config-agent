"""Persistent Chroma vector store using caller-provided embeddings."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing, suppress
from pathlib import Path
from typing import Any

import chromadb

from core.types import ChunkRecord
from libs.vector_store.base_vector_store import BaseVectorStore

_FULL_METADATA_KEY = "_full_metadata_json"


class ChromaStore(BaseVectorStore):
    def __init__(
        self,
        *,
        persist_path: str | Path,
        collection_name: str,
        read_only: bool = False,
    ) -> None:
        self.persist_path = Path(persist_path)
        self.collection_name = collection_name
        self.read_only = read_only
        collection_key = hashlib.sha256(collection_name.encode("utf-8")).hexdigest()[:16]
        self.collection_key = collection_key
        self.swap_journal_path = self.persist_path / f".{collection_key}.chroma-swap.json"
        if read_only:
            # Management reads use SQLite's lock-aware read-only connection in _read_records.
            # Constructing PersistentClient/get_or_create_collection is intentionally avoided.
            self.client = None
            self.collection = None
        else:
            self.persist_path.mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=self.persist_path)
            self._wait_for_active_swap()
            self.collection = self.client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=None,
            )
            self._prune_backup_generations()

    def upsert(self, records: list[ChunkRecord], trace: Any | None = None) -> None:
        del trace
        self._ensure_writable()
        self._ensure_active_generation()
        if not records:
            return
        if any(record.dense_vector is None for record in records):
            raise ValueError("All vector store records must contain dense_vector")
        self.collection.upsert(
            ids=[record.id for record in records],
            embeddings=[record.dense_vector for record in records],
            documents=[record.text for record in records],
            metadatas=[self._flatten_metadata(record.metadata) for record in records],
        )

    def replace_all_atomically(
        self,
        records: list[ChunkRecord],
        *,
        finalize: Callable[[], None] | None = None,
    ) -> None:
        """Stage a complete corpus and switch collections with rollback support.

        Chroma fixes vector dimensionality when a collection receives its first vector.
        A temporary collection therefore lets Local LSA change its effective dimensions
        without destroying the query-compatible collection first. ``finalize`` runs
        while the previous collection remains available as a rollback target.
        """

        self._ensure_writable()
        if any(record.dense_vector is None for record in records):
            raise ValueError("All vector store records must contain dense_vector")
        transaction_id = uuid.uuid4().hex
        staging_name = f"wms-stage-{self.collection_key}-{transaction_id}"
        backup_name = f"wms-backup-{self.collection_key}-{transaction_id}"
        failed_name = f"wms-failed-{self.collection_key}-{transaction_id}"
        journal = {
            "pid": os.getpid(),
            "collection": self.collection_name,
            "staging": staging_name,
            "backup": backup_name,
            "phase": "preparing",
        }
        self._write_swap_journal(journal)
        staging = None
        target = self.collection
        target_renamed = False
        switched = False
        transaction_resolved = False
        try:
            staging = self.client.create_collection(
                name=staging_name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=None,
            )
            self._upsert_collection(staging, records)
            if staging.count() != len(records):
                raise RuntimeError("Chroma staging collection count does not match input corpus")
            journal["phase"] = "staged"
            self._write_swap_journal(journal)
            target.modify(name=backup_name)
            target_renamed = True
            journal["phase"] = "backup_renamed"
            self._write_swap_journal(journal)
            staging.modify(name=self.collection_name)
            switched = True
            self.collection = staging
            journal["phase"] = "switched"
            self._write_swap_journal(journal)
            if finalize is not None:
                finalize()
            journal["phase"] = "finalized"
            self._write_swap_journal(journal)
        except Exception:
            if switched:
                try:
                    staging.modify(name=failed_name)
                except Exception:
                    # The staged collection is disposable; release the canonical name
                    # before restoring the retained query-compatible backup.
                    self.client.delete_collection(name=self.collection_name)
                target.modify(name=self.collection_name)
                self.collection = target
                with suppress(Exception):
                    self.client.delete_collection(name=failed_name)
            elif target_renamed:
                target.modify(name=self.collection_name)
                self.collection = target
            with suppress(Exception):
                self.client.delete_collection(name=staging_name)
            transaction_resolved = True
            raise
        else:
            transaction_resolved = True
        finally:
            if transaction_resolved:
                self.swap_journal_path.unlink(missing_ok=True)
        with suppress(Exception):
            self.client.delete_collection(name=backup_name)

    @classmethod
    def _upsert_collection(cls, collection: Any, records: list[ChunkRecord]) -> None:
        for start in range(0, len(records), 256):
            batch = records[start : start + 256]
            if not batch:
                continue
            collection.upsert(
                ids=[record.id for record in batch],
                embeddings=[record.dense_vector for record in batch],
                documents=[record.text for record in batch],
                metadatas=[cls._flatten_metadata(record.metadata) for record in batch],
            )

    def _wait_for_active_swap(self) -> None:
        deadline = time.monotonic() + 60.0
        while self.swap_journal_path.is_file():
            try:
                journal = json.loads(self.swap_journal_path.read_text(encoding="utf-8"))
                owner_pid = int(journal.get("pid", -1))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Unreadable Chroma swap journal: {self.swap_journal_path}"
                ) from exc
            if not self._process_is_alive(owner_pid):
                raise RuntimeError(
                    "Interrupted Chroma corpus swap detected; refusing to create or open "
                    f"collection until recovered: {self.swap_journal_path}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Chroma corpus swap: {self.swap_journal_path}"
                )
            time.sleep(0.05)

    def _write_swap_journal(self, values: dict[str, Any]) -> None:
        temporary = self.swap_journal_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(values, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.swap_journal_path)

    def _prune_backup_generations(self) -> None:
        prefix = f"wms-backup-{self.collection_key}-"
        for collection in self.client.list_collections():
            if collection.name.startswith(prefix):
                with suppress(Exception):
                    self.client.delete_collection(name=collection.name)

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process = kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return ctypes.get_last_error() == 5
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(process)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def query(
        self,
        vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
        trace: Any | None = None,
    ) -> list[dict[str, Any]]:
        del trace
        if not vector:
            raise ValueError("query vector must not be empty")
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("query vector must contain only finite values")
        if math.sqrt(sum(value * value for value in vector)) <= 1e-12:
            return []
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0")
        if self.read_only:
            raise NotImplementedError(
                "Vector similarity query is unavailable in strict Chroma read-only mode"
            )
        self._ensure_active_generation()
        if self.count() == 0:
            return []
        result = self.collection.query(
            query_embeddings=[vector],
            n_results=min(top_k, self.count()),
            where=filters,
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            {
                "id": chunk_id,
                "text": document or "",
                "metadata": self._restore_metadata(metadata or {}),
                "distance": float(distance),
                "score": 1.0 - float(distance),
            }
            for chunk_id, document, metadata, distance in zip(
                ids, documents, metadatas, distances, strict=True
            )
        ]

    def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        records = {record["id"]: record for record in self._read_records()}
        return [records[chunk_id] for chunk_id in ids if chunk_id in records]

    def get_by_metadata(
        self,
        filters: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return records matching scalar metadata filters without exposing embeddings."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than 0")
        if offset < 0:
            raise ValueError("offset must be greater than or equal to 0")
        records = self._read_records()
        if filters:
            records = [
                record
                for record in records
                if all(record["metadata"].get(key) == value for key, value in filters.items())
            ]
        end = None if limit is None else offset + limit
        return records[offset:end]

    def delete_by_metadata(self, filters: dict[str, Any]) -> int:
        """Delete matching records and return the number that existed beforehand."""
        self._ensure_writable()
        if not filters:
            raise ValueError("filters must not be empty for metadata deletion")
        records = self.get_by_metadata(filters)
        self.delete([record["id"] for record in records])
        return len(records)

    def get_collection_stats(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return privacy-safe collection and document counts for management views."""
        records = self.get_by_metadata(filters)
        documents = {
            str(record["metadata"].get("source_path", ""))
            for record in records
            if record["metadata"].get("source_path")
        }
        collections = sorted(
            {str(record["metadata"].get("collection", "default")) for record in records}
        )
        return {
            "collection_name": self.collection_name,
            "chunk_count": len(records),
            "document_count": len(documents),
            "collections": collections,
        }

    def count(self) -> int:
        return len(self._read_records(include_metadata=False))

    def list_ids(self) -> list[str]:
        return [record["id"] for record in self._read_records(include_metadata=False)]

    def delete(self, ids: list[str]) -> None:
        self._ensure_writable()
        self._ensure_active_generation()
        for start in range(0, len(ids), 500):
            self.collection.delete(ids=ids[start : start + 500])

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise PermissionError("ChromaStore is read-only")

    def _ensure_active_generation(self) -> None:
        """Reject stale workers instead of mixing old Dense vectors with a new model/BM25."""

        active = self.client.get_collection(name=self.collection_name)
        if active.id != self.collection.id:
            raise RuntimeError(
                "Chroma collection generation changed; recreate the query/index worker "
                "to load the matching embedding model"
            )

    def refresh_active_generation(self) -> str:
        """Refresh a writer handle after it acquires the shared lifecycle lock."""

        self._ensure_writable()
        self.collection = self.client.get_collection(name=self.collection_name)
        return str(self.collection.id)

    def _read_records(self, *, include_metadata: bool = True) -> list[dict[str, Any]]:
        """Read Chroma's version-pinned SQLite metadata segment without native HNSW calls.

        Chroma 1.x on Windows can terminate the interpreter while counting some persisted
        collections. Management views do not need embeddings, so this Chroma 1.5.x compatibility
        adapter keeps reads deterministic and prevents that native crash. The project dependency
        is deliberately pinned to the matching minor series because this is not a public API.
        """
        database_path = self.persist_path / "chroma.sqlite3"
        if not database_path.is_file():
            return []
        uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT e.id, e.embedding_id
                    FROM embeddings AS e
                    JOIN segments AS s ON s.id = e.segment_id
                    JOIN collections AS c ON c.id = s.collection
                    WHERE c.name = ? AND s.scope = 'METADATA'
                    ORDER BY e.id
                    """,
                    (self.collection_name,),
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(
                    "Unsupported or corrupt Chroma metadata schema; expected Chroma 1.5.x"
                ) from exc
            records = [
                {"id": str(embedding_id), "text": "", "metadata": {}} for _, embedding_id in rows
            ]
            if not include_metadata or not rows:
                return records
            record_by_internal_id = {
                int(internal_id): record
                for (internal_id, _), record in zip(rows, records, strict=True)
            }
            metadata_rows: list[tuple[Any, ...]] = []
            internal_ids = [int(row[0]) for row in rows]
            for start in range(0, len(internal_ids), 500):
                batch = internal_ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                metadata_rows.extend(
                    connection.execute(
                        f"""
                        SELECT id, key, string_value, int_value, float_value, bool_value
                        FROM embedding_metadata
                        WHERE id IN ({placeholders})
                        ORDER BY id, key
                        """,
                        batch,
                    ).fetchall()
                )
        for internal_id, key, string_value, int_value, float_value, bool_value in metadata_rows:
            record = record_by_internal_id[int(internal_id)]
            value = self._metadata_value(string_value, int_value, float_value, bool_value)
            if key == "chroma:document":
                record["text"] = value or ""
            else:
                record["metadata"][str(key)] = value
        for record in records:
            record["metadata"] = self._restore_metadata(record["metadata"])
        return records

    @staticmethod
    def _metadata_value(
        string_value: str | None,
        int_value: int | None,
        float_value: float | None,
        bool_value: int | None,
    ) -> str | int | float | bool | None:
        if string_value is not None:
            return string_value
        if int_value is not None:
            return int_value
        if float_value is not None:
            return float_value
        if bool_value is not None:
            return bool(bool_value)
        return None

    @staticmethod
    def _flatten_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
        flattened: dict[str, str | int | float | bool] = {
            _FULL_METADATA_KEY: json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        }
        for key, value in metadata.items():
            if isinstance(value, str | int | float | bool):
                flattened[key] = value
        return flattened

    @staticmethod
    def _restore_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        serialized = metadata.get(_FULL_METADATA_KEY)
        if isinstance(serialized, str):
            try:
                restored = json.loads(serialized)
            except (json.JSONDecodeError, TypeError):
                restored = {
                    key: value for key, value in metadata.items() if key != _FULL_METADATA_KEY
                }
        else:
            restored = {key: value for key, value in metadata.items() if key != _FULL_METADATA_KEY}
        for key in ("embedding_signature", "content_hash"):
            if key in metadata:
                restored[key] = metadata[key]
        return restored
