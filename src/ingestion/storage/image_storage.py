"""Content-addressed image files with a persistent SQLite lookup index."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


class ImageStorage:
    """Store image bytes idempotently and map logical image IDs to local paths."""

    def __init__(
        self,
        root_path: str | Path = "data/images",
        database_path: str | Path = "data/db/image_index.db",
    ) -> None:
        self.root_path = Path(root_path)
        self.database_path = Path(database_path)
        self.root_path.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

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
        if not destination.is_file():
            temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(data)
            if destination.is_file():
                temporary.unlink(missing_ok=True)
            else:
                temporary.replace(destination)
        self._upsert(
            image_id=image_id,
            file_path=destination.resolve(),
            collection=collection,
            doc_hash=doc_hash,
            page_num=page_num,
        )
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

    def get_path(self, image_id: str) -> Path | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT file_path FROM image_index WHERE image_id = ?", (image_id,)
            ).fetchone()
        return Path(row[0]) if row else None

    def list_collection(self, collection: str) -> dict[str, Path]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT image_id, file_path FROM image_index "
                "WHERE collection = ? ORDER BY image_id",
                (collection,),
            ).fetchall()
        return {str(image_id): Path(file_path) for image_id, file_path in rows}

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM image_index").fetchone()
        return int(row[0]) if row else 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS image_index (
                    image_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    collection TEXT,
                    doc_hash TEXT,
                    page_num INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_image_collection "
                "ON image_index(collection)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_image_doc_hash ON image_index(doc_hash)"
            )

    def _upsert(
        self,
        *,
        image_id: str,
        file_path: Path,
        collection: str,
        doc_hash: str | None,
        page_num: int | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO image_index (
                    image_id, file_path, collection, doc_hash, page_num
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(image_id) DO UPDATE SET
                    file_path = excluded.file_path,
                    collection = excluded.collection,
                    doc_hash = excluded.doc_hash,
                    page_num = excluded.page_num
                """,
                (image_id, str(file_path), collection, doc_hash, page_num),
            )

    @staticmethod
    def _safe_collection(collection: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", collection.strip()).strip(".-")
        if not safe:
            raise ValueError("collection must contain a safe path component")
        return safe
