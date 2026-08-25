"""File hashing and SQLite-backed ingestion history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from libs.sqlite_snapshot import connect_sqlite_snapshot


@dataclass(frozen=True, slots=True)
class IngestionRecord:
    file_hash: str
    file_path: str
    status: str
    processed_at: str
    error_msg: str | None
    metadata: dict[str, Any]
    collection: str | None = None


class FileIntegrityChecker(ABC):
    """Contract for detecting documents that were already ingested successfully."""

    @abstractmethod
    def compute_sha256(self, path: str | Path) -> str:
        """Return the SHA256 digest for a file."""

    @abstractmethod
    def should_skip(
        self,
        file_hash: str,
        processing_signature: str | None = None,
        *,
        collection: str | None = None,
    ) -> bool:
        """Return whether a file hash has a successful ingestion record."""

    @abstractmethod
    def mark_success(self, file_hash: str, file_path: str | Path, **metadata: Any) -> None:
        """Persist a successful ingestion result."""

    @abstractmethod
    def mark_failed(
        self,
        file_hash: str,
        error_msg: str,
        file_path: str | Path | None = None,
        *,
        collection: str | None = None,
    ) -> None:
        """Persist a failed ingestion result."""


class SQLiteIntegrityChecker(FileIntegrityChecker):
    """SQLite implementation using WAL and one connection per operation."""

    def __init__(
        self,
        database_path: str | Path = "data/db/ingestion_history.db",
        *,
        read_only: bool = False,
    ) -> None:
        self.database_path = Path(database_path)
        self.read_only = read_only
        if not read_only:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_schema()
        self._collection_column = self._has_collection_column()

    def compute_sha256(self, path: str | Path) -> str:
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Cannot hash missing file: {file_path}")
        digest = hashlib.sha256()
        with file_path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def should_skip(
        self,
        file_hash: str,
        processing_signature: str | None = None,
        *,
        collection: str | None = None,
    ) -> bool:
        if not self.database_path.is_file():
            return False
        clauses = ["file_hash = ?"]
        parameters = [file_hash]
        if collection is not None and self._collection_column:
            clauses.append("collection = ?")
            parameters.append(collection)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status, metadata_json FROM ingestion_history "
                f"WHERE {' AND '.join(clauses)} ORDER BY processed_at DESC LIMIT 1",
                parameters,
            ).fetchone()
        if row is None or row[0] != "success":
            return False
        if collection is not None and not self._collection_column:
            try:
                legacy_metadata = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                return False
            if legacy_metadata.get("collection") != collection:
                return False
        if processing_signature is None:
            return True
        try:
            metadata = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            return False
        return metadata.get("processing_signature") == processing_signature

    def mark_success(self, file_hash: str, file_path: str | Path, **metadata: Any) -> None:
        self._ensure_writable()
        self._upsert(
            file_hash=file_hash,
            file_path=str(file_path),
            status="success",
            error_msg=None,
            metadata=metadata,
        )

    def mark_failed(
        self,
        file_hash: str,
        error_msg: str,
        file_path: str | Path | None = None,
        *,
        collection: str | None = None,
    ) -> None:
        self._ensure_writable()
        existing_path, metadata = self._existing_values(file_hash, collection)
        if collection is not None:
            metadata["collection"] = collection
        self._upsert(
            file_hash=file_hash,
            file_path=str(file_path) if file_path is not None else existing_path,
            status="failed",
            error_msg=error_msg,
            metadata=metadata,
        )

    def list_processed(
        self,
        *,
        status: str | None = "success",
        collection: str | None = None,
    ) -> list[IngestionRecord]:
        """Return ingestion history newest-first, tolerating legacy metadata."""
        if not self.database_path.is_file():
            return []
        select = "file_hash, file_path, status, processed_at, error_msg, metadata_json"
        if self._collection_column:
            select += ", collection"
        query = f"SELECT {select} FROM ingestion_history"
        clauses: list[str] = []
        parameters: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if collection is not None and self._collection_column:
            clauses.append("collection = ?")
            parameters.append(collection)
        if clauses:
            query += f" WHERE {' AND '.join(clauses)}"
        query += " ORDER BY processed_at DESC, file_path"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()

        records: list[IngestionRecord] = []
        for row in rows:
            try:
                metadata = json.loads(row[5])
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            record_collection = (
                str(row[6]) if self._collection_column and row[6] else metadata.get("collection")
            )
            if collection is not None and record_collection != collection:
                continue
            records.append(
                IngestionRecord(
                    file_hash=str(row[0]),
                    file_path=str(row[1]),
                    status=str(row[2]),
                    processed_at=str(row[3]),
                    error_msg=str(row[4]) if row[4] is not None else None,
                    metadata=metadata,
                    collection=str(record_collection) if record_collection else None,
                )
            )
        return records

    def remove_record(
        self,
        *,
        file_hash: str | None = None,
        file_path: str | Path | None = None,
        collection: str | None = None,
    ) -> int:
        """Remove one or more history records selected by a stable hash or exact path."""
        self._ensure_writable()
        if file_hash is None and file_path is None:
            raise ValueError("file_hash or file_path is required")
        clauses: list[str] = []
        parameters: list[str] = []
        if file_hash is not None:
            clauses.append("file_hash = ?")
            parameters.append(file_hash)
        if file_path is not None:
            clauses.append("file_path = ?")
            parameters.append(str(file_path))
        if collection is not None:
            clauses.append("collection = ?")
            parameters.append(collection)
        with self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM ingestion_history WHERE {' AND '.join(clauses)}", parameters
            )
            removed = max(cursor.rowcount, 0)
        self._checkpoint()
        return removed

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = connect_sqlite_snapshot(self.database_path)
        else:
            connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back the operation and always release SQLite sidecar handles."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        for attempt in range(8):
            connection = sqlite3.connect(self.database_path, timeout=30)
            connection.execute("PRAGMA busy_timeout=30000")
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE")
                self._initialize_schema_locked(connection)
                connection.commit()
                break
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.rollback()
                if "locked" not in str(exc).casefold() or attempt == 7:
                    raise
                time.sleep(0.02 * (2**attempt))
            finally:
                connection.close()
        self._checkpoint()

    def _initialize_schema_locked(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(ingestion_history)").fetchall()
        }
        if not columns:
            self._create_schema(connection)
            return
        if "record_id" in columns and "collection" in columns:
            return
        legacy_rows = connection.execute(
            "SELECT file_hash, file_path, status, processed_at, error_msg, metadata_json "
            "FROM ingestion_history"
        ).fetchall()
        connection.execute("ALTER TABLE ingestion_history RENAME TO ingestion_history_legacy")
        self._create_schema(connection)
        for row in legacy_rows:
            try:
                metadata = json.loads(row[5])
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            collection = metadata.get("collection") or "wms-system-training"
            collection_value = str(collection)
            connection.execute(
                """
                INSERT INTO ingestion_history (
                    record_id, file_hash, collection, file_path, status,
                    processed_at, error_msg, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._record_id(str(row[0]), collection_value),
                    str(row[0]),
                    collection_value,
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    row[4],
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
        connection.execute("DROP TABLE ingestion_history_legacy")

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE ingestion_history (
                record_id TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                collection TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
                processed_at TEXT NOT NULL,
                error_msg TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_ingestion_history_hash ON ingestion_history(file_hash)"
        )
        connection.execute(
            "CREATE INDEX idx_ingestion_history_collection ON ingestion_history(collection)"
        )

    def _upsert(
        self,
        *,
        file_hash: str,
        file_path: str,
        status: str,
        error_msg: str | None,
        metadata: dict[str, Any],
    ) -> None:
        self._ensure_writable()
        if not file_hash:
            raise ValueError("file_hash must not be empty")
        processed_at = datetime.now(UTC).isoformat()
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        collection = metadata.get("collection")
        collection_value = str(collection) if collection else ""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO ingestion_history (
                    record_id, file_hash, collection, file_path, status,
                    processed_at, error_msg, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    file_path = excluded.file_path,
                    status = excluded.status,
                    processed_at = excluded.processed_at,
                    error_msg = excluded.error_msg,
                    metadata_json = excluded.metadata_json
                """,
                (
                    self._record_id(file_hash, collection_value),
                    file_hash,
                    collection_value,
                    file_path,
                    status,
                    processed_at,
                    error_msg,
                    metadata_json,
                ),
            )
        self._checkpoint()

    def _has_collection_column(self) -> bool:
        if not self.database_path.is_file():
            return False
        if self.read_only:
            connection = connect_sqlite_snapshot(self.database_path)
        else:
            connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            with connection:
                columns = connection.execute("PRAGMA table_info(ingestion_history)").fetchall()
        finally:
            connection.close()
        return any(str(row[1]) == "collection" for row in columns)

    def _existing_values(
        self,
        file_hash: str,
        collection: str | None,
    ) -> tuple[str, dict[str, Any]]:
        if not self.database_path.is_file():
            return "", {}
        with self._connection() as connection:
            if collection is None:
                row = connection.execute(
                    "SELECT file_path, metadata_json FROM ingestion_history "
                    "WHERE file_hash = ? ORDER BY processed_at DESC LIMIT 1",
                    (file_hash,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT file_path, metadata_json FROM ingestion_history WHERE record_id = ?",
                    (self._record_id(file_hash, collection),),
                ).fetchone()
        if row is None:
            return "", {}
        try:
            metadata = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return str(row[0]), metadata if isinstance(metadata, dict) else {}

    def _checkpoint(self) -> None:
        """Move committed WAL pages into the main DB for management readers."""
        if self.read_only or not self.database_path.is_file():
            return
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    @staticmethod
    def _record_id(file_hash: str, collection: str) -> str:
        return hashlib.sha256(f"{file_hash}\0{collection}".encode()).hexdigest()

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise PermissionError("SQLiteIntegrityChecker is read-only")
