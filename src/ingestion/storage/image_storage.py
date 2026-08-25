"""Content-addressed image files with a persistent SQLite lookup index."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from libs.atomic_file import replace_file_atomically
from libs.sqlite_snapshot import connect_sqlite_snapshot

_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


@dataclass(frozen=True, slots=True)
class StoredImage:
    image_id: str
    file_path: Path
    collection: str
    doc_hash: str | None
    page_num: int | None
    created_at: str


class ImageStorage:
    """Store image bytes idempotently and map logical image IDs to local paths."""

    def __init__(
        self,
        root_path: str | Path = "data/images",
        database_path: str | Path = "data/db/image_index.db",
        *,
        read_only: bool = False,
    ) -> None:
        self.root_path = Path(root_path)
        self.database_path = Path(database_path)
        self.read_only = read_only
        if not read_only:
            self.root_path.mkdir(parents=True, exist_ok=True)
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_schema()
            self._purge_delete_queue()

    def save_bytes(
        self,
        image_id: str,
        data: bytes,
        *,
        collection: str,
        extension: str = ".bin",
        doc_hash: str | None = None,
        page_num: int | None = None,
    ) -> Path:
        self._ensure_writable()
        if not image_id.strip():
            raise ValueError("image_id must not be empty")
        if not data:
            raise ValueError("image data must not be empty")
        collection_dir = self.root_path / self._safe_collection(collection)
        collection_dir.mkdir(parents=True, exist_ok=True)
        suffix = extension.lower()
        if not _SAFE_SUFFIX.fullmatch(suffix):
            suffix = ".bin"
        digest = hashlib.sha256(data).hexdigest()
        destination = collection_dir / f"{digest}{suffix}"
        temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
        created = False
        cleanup_queued = False
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not destination.is_file():
                    temporary.write_bytes(data)
                    replace_file_atomically(temporary, destination)
                    created = True
                cleanup_queued = self._upsert(
                    connection,
                    image_id=image_id,
                    file_path=destination.resolve(),
                    collection=collection,
                    doc_hash=doc_hash,
                    page_num=page_num,
                )
        except Exception:
            if created:
                self._queue_file_if_unreferenced(destination.resolve())
            raise
        finally:
            temporary.unlink(missing_ok=True)
        self._checkpoint()
        if cleanup_queued:
            self._purge_delete_queue()
        return destination.resolve()

    def store_file(
        self,
        image_id: str,
        source_path: str | Path,
        *,
        collection: str,
        doc_hash: str | None = None,
        page_num: int | None = None,
    ) -> Path:
        self._ensure_writable()
        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError(f"Image file does not exist: {source}")
        return self.save_bytes(
            image_id,
            source.read_bytes(),
            collection=collection,
            extension=source.suffix or ".bin",
            doc_hash=doc_hash,
            page_num=page_num,
        )

    def store_metadata_images(
        self,
        images: list[dict[str, Any]],
        *,
        collection: str,
        doc_hash: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_writable()
        stored: list[dict[str, Any]] = []
        for original in images:
            image = dict(original)
            image_id = image.get("id")
            path = image.get("path")
            if isinstance(image_id, str) and isinstance(path, str):
                destination = self.store_file(
                    image_id,
                    path,
                    collection=collection,
                    doc_hash=doc_hash,
                    page_num=image.get("page") if isinstance(image.get("page"), int) else None,
                )
                image["path"] = destination.as_posix()
            stored.append(image)
        return stored

    def get_path(self, image_id: str, *, collection: str | None = None) -> Path | None:
        if not self.database_path.is_file():
            return None
        query = "SELECT file_path FROM image_index WHERE image_id = ?"
        parameters = [image_id]
        if collection is not None:
            query += " AND collection = ?"
            parameters.append(collection)
        query += " ORDER BY collection"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        if collection is None and len(rows) > 1:
            raise ValueError("image_id exists in multiple collections; collection is required")
        return Path(rows[0][0]) if rows else None

    def list_collection(self, collection: str) -> dict[str, Path]:
        if not self.database_path.is_file():
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT image_id, file_path FROM image_index "
                "WHERE collection = ? ORDER BY image_id",
                (collection,),
            ).fetchall()
        return {str(image_id): Path(file_path) for image_id, file_path in rows}

    def list_images(
        self,
        *,
        collection: str | None = None,
        doc_hash: str | None = None,
    ) -> list[StoredImage]:
        """List indexed images using optional collection and document filters."""
        if not self.database_path.is_file():
            return []
        clauses: list[str] = []
        parameters: list[str] = []
        if collection is not None:
            clauses.append("collection = ?")
            parameters.append(collection)
        if doc_hash is not None:
            clauses.append("doc_hash = ?")
            parameters.append(doc_hash)
        query = (
            "SELECT image_id, file_path, collection, doc_hash, page_num, created_at "
            "FROM image_index"
        )
        if clauses:
            query += f" WHERE {' AND '.join(clauses)}"
        query += " ORDER BY collection, image_id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            StoredImage(
                image_id=str(row[0]),
                file_path=Path(row[1]),
                collection=str(row[2] or "default"),
                doc_hash=str(row[3]) if row[3] is not None else None,
                page_num=int(row[4]) if row[4] is not None else None,
                created_at=str(row[5]),
            )
            for row in rows
        ]

    def remove_document(self, doc_hash: str, *, collection: str | None = None) -> int:
        """Remove mappings and durably queue unreferenced files for safe deletion."""
        self._ensure_writable()
        if not doc_hash.strip():
            raise ValueError("doc_hash must not be empty")
        clauses = ["doc_hash = ?"]
        parameters = [doc_hash]
        if collection is not None:
            clauses.append("collection = ?")
            parameters.append(collection)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT file_path FROM image_index WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchall()
            if not rows:
                return 0
            connection.execute(f"DELETE FROM image_index WHERE {' AND '.join(clauses)}", parameters)
            for file_path in {Path(str(row[0])) for row in rows}:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM image_index WHERE file_path = ?", (str(file_path),)
                ).fetchone()
                if remaining and int(remaining[0]) == 0:
                    connection.execute(
                        "INSERT OR IGNORE INTO image_delete_queue (file_path) VALUES (?)",
                        (str(file_path),),
                    )
        self._checkpoint()
        self._purge_delete_queue()
        return len(rows)

    def pending_cleanup_count(self) -> int:
        """Return unreferenced files retained for a later safe cleanup retry."""

        if not self.database_path.is_file():
            return 0
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT COUNT(*) FROM image_delete_queue").fetchone()
        except sqlite3.OperationalError:
            # Strict read-only access also supports image indexes created before the queue table.
            return 0
        return int(row[0]) if row else 0

    def count(self) -> int:
        if not self.database_path.is_file():
            return 0
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) FROM image_index").fetchone()
        return int(row[0]) if row else 0

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
        columns = connection.execute("PRAGMA table_info(image_index)").fetchall()
        primary_key = [
            str(row[1]) for row in sorted(columns, key=lambda row: int(row[5])) if row[5]
        ]
        if columns and primary_key != ["collection", "image_id"]:
            connection.execute("ALTER TABLE image_index RENAME TO image_index_legacy")
            self._create_schema(connection)
            connection.execute(
                """
                INSERT INTO image_index (
                    image_id, file_path, collection, doc_hash, page_num, created_at
                )
                SELECT image_id, file_path,
                       COALESCE(NULLIF(collection, ''), 'default'),
                       doc_hash, page_num, created_at
                FROM image_index_legacy
                """
            )
            connection.execute("DROP TABLE image_index_legacy")
        elif not columns:
            self._create_schema(connection)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_image_collection ON image_index(collection)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_image_doc_hash ON image_index(doc_hash)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS image_delete_queue (
                file_path TEXT PRIMARY KEY,
                queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE image_index (
                image_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                collection TEXT NOT NULL DEFAULT 'default',
                doc_hash TEXT,
                page_num INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (collection, image_id)
            )
            """
        )

    def _is_managed_file(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root_path.resolve())
        except ValueError:
            return False
        return True

    def _upsert(
        self,
        connection: sqlite3.Connection,
        *,
        image_id: str,
        file_path: Path,
        collection: str,
        doc_hash: str | None,
        page_num: int | None,
    ) -> bool:
        self._ensure_writable()
        previous = connection.execute(
            "SELECT file_path FROM image_index WHERE collection = ? AND image_id = ?",
            (collection, image_id),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO image_index (
                image_id, file_path, collection, doc_hash, page_num
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(collection, image_id) DO UPDATE SET
                file_path = excluded.file_path,
                doc_hash = excluded.doc_hash,
                page_num = excluded.page_num
            """,
            (image_id, str(file_path), collection, doc_hash, page_num),
        )
        connection.execute(
            "DELETE FROM image_delete_queue WHERE file_path = ?",
            (str(file_path),),
        )
        previous_path = str(previous[0]) if previous else None
        if previous_path is None or previous_path == str(file_path):
            return False
        references = connection.execute(
            "SELECT COUNT(*) FROM image_index WHERE file_path = ?",
            (previous_path,),
        ).fetchone()
        if references and int(references[0]) > 0:
            return False
        connection.execute(
            "INSERT OR IGNORE INTO image_delete_queue (file_path) VALUES (?)",
            (previous_path,),
        )
        return True

    def _queue_file_if_unreferenced(self, file_path: Path) -> None:
        """Recover a physical file created by a rolled-back mapping transaction."""

        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                references = connection.execute(
                    "SELECT COUNT(*) FROM image_index WHERE file_path = ?",
                    (str(file_path),),
                ).fetchone()
                if not references or int(references[0]) == 0:
                    connection.execute(
                        "INSERT OR IGNORE INTO image_delete_queue (file_path) VALUES (?)",
                        (str(file_path),),
                    )
        except sqlite3.Error:
            # Preserve the content-addressed file rather than risk deleting a concurrent mapping.
            return
        self._purge_delete_queue()

    def _purge_delete_queue(self) -> None:
        """Delete queued files one transaction at a time without restoring broken mappings.

        A failed unlink leaves a durable queue row. A later writable construction retries it;
        successful prior deletions stay committed, and a newly referenced content-addressed file
        is removed from the queue instead of being deleted.
        """

        if self.read_only or not self.database_path.is_file():
            return
        with self._connection() as connection:
            queued = [
                Path(str(row[0]))
                for row in connection.execute(
                    "SELECT file_path FROM image_delete_queue ORDER BY file_path"
                ).fetchall()
            ]
        for file_path in queued:
            try:
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    references = connection.execute(
                        "SELECT COUNT(*) FROM image_index WHERE file_path = ?",
                        (str(file_path),),
                    ).fetchone()
                    if references and int(references[0]) > 0:
                        connection.execute(
                            "DELETE FROM image_delete_queue WHERE file_path = ?",
                            (str(file_path),),
                        )
                        continue
                    if self._is_managed_file(file_path):
                        file_path.unlink(missing_ok=True)
                    connection.execute(
                        "DELETE FROM image_delete_queue WHERE file_path = ?",
                        (str(file_path),),
                    )
            except OSError:
                # The committed mapping deletion is safe. Keep the queue row for a future retry.
                continue
        self._checkpoint()

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

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise PermissionError("ImageStorage is read-only")

    @staticmethod
    def _safe_collection(collection: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", collection.strip()).strip(".-")
        if not safe:
            raise ValueError("collection must contain a safe path component")
        return safe
