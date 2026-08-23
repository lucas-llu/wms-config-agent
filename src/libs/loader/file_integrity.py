"""File hashing and SQLite-backed ingestion history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class FileIntegrityChecker(ABC):
    """Contract for detecting documents that were already ingested successfully."""

    @abstractmethod
    def compute_sha256(self, path: str | Path) -> str:
        """Return the SHA256 digest for a file."""

    @abstractmethod
    def should_skip(self, file_hash: str) -> bool:
        """Return whether a file hash has a successful ingestion record."""

    @abstractmethod
    def mark_success(
        self, file_hash: str, file_path: str | Path, **metadata: Any
    ) -> None:
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

    def should_skip(self, file_hash: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM ingestion_history WHERE file_hash = ?", (file_hash,)
            ).fetchone()
        return row is not None and row[0] == "success"

    def mark_success(
        self, file_hash: str, file_path: str | Path, **metadata: Any
    ) -> None:
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
