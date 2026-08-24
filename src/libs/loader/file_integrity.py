"""File hashing and SQLite-backed ingestion history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class IngestionRecord:
    file_hash: str
    file_path: str
    status: str
    processed_at: str
    error_msg: str | None
    metadata: dict[str, Any]


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
    ) -> bool:
        """Return whether a file hash has a successful ingestion record."""

    @abstractmethod
    def mark_success(self, file_hash: str, file_path: str | Path, **metadata: Any) -> None:
        """Persist a successful ingestion result."""

    @abstractmethod
    def mark_failed(
        self, file_hash: str, error_msg: str, file_path: str | Path | None = None
    ) -> None:
        """Persist a failed ingestion result."""


class SQLiteIntegrityChecker(FileIntegrityChecker):
    """SQLite implementation using WAL and one connection per operation."""

    def __init__(self, database_path: str | Path = "data/db/ingestion_history.db") -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

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
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, metadata_json FROM ingestion_history WHERE file_hash = ?",
                (file_hash,),
            ).fetchone()
        if row is None or row[0] != "success":
            return False
        if processing_signature is None:
            return True
        try:
            metadata = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            return False
        return metadata.get("processing_signature") == processing_signature

    def mark_success(self, file_hash: str, file_path: str | Path, **metadata: Any) -> None:
        self._upsert(
            file_hash=file_hash,
            file_path=str(file_path),
            status="success",
            error_msg=None,
            metadata=metadata,
        )

    def mark_failed(
        self, file_hash: str, error_msg: str, file_path: str | Path | None = None
    ) -> None:
        self._upsert(
            file_hash=file_hash,
            file_path=str(file_path) if file_path is not None else "",
            status="failed",
            error_msg=error_msg,
            metadata={},
        )

    def list_processed(
        self,
        *,
        status: str | None = "success",
        collection: str | None = None,
    ) -> list[IngestionRecord]:
        """Return ingestion history newest-first, tolerating legacy metadata."""
        query = (
            "SELECT file_hash, file_path, status, processed_at, error_msg, metadata_json "
            "FROM ingestion_history"
        )
        parameters: list[str] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY processed_at DESC, file_path"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()

        records: list[IngestionRecord] = []
        for row in rows:
            try:
                metadata = json.loads(row[5])
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            if collection is not None and metadata.get("collection") != collection:
                continue
            records.append(
                IngestionRecord(
                    file_hash=str(row[0]),
                    file_path=str(row[1]),
                    status=str(row[2]),
                    processed_at=str(row[3]),
                    error_msg=str(row[4]) if row[4] is not None else None,
                    metadata=metadata,
                )
            )
        return records

    def remove_record(
        self,
        *,
        file_hash: str | None = None,
        file_path: str | Path | None = None,
    ) -> int:
        """Remove one or more history records selected by a stable hash or exact path."""
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
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM ingestion_history WHERE {' OR '.join(clauses)}", parameters
            )
        return max(cursor.rowcount, 0)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_history (
                    file_hash TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
                    processed_at TEXT NOT NULL,
                    error_msg TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
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
        if not file_hash:
            raise ValueError("file_hash must not be empty")
        processed_at = datetime.now(UTC).isoformat()
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ingestion_history (
                    file_hash, file_path, status, processed_at, error_msg, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_hash) DO UPDATE SET
                    file_path = excluded.file_path,
                    status = excluded.status,
                    processed_at = excluded.processed_at,
                    error_msg = excluded.error_msg,
                    metadata_json = excluded.metadata_json
                """,
                (
                    file_hash,
                    file_path,
                    status,
                    processed_at,
                    error_msg,
                    metadata_json,
                ),
            )
