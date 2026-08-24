"""Persistent Chroma vector store using caller-provided embeddings."""

from __future__ import annotations

import json
import math
import sqlite3
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
    ) -> None:
        self.persist_path = Path(persist_path)
        self.collection_name = collection_name
        self.persist_path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=self.persist_path)
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )

    def upsert(self, records: list[ChunkRecord], trace: Any | None = None) -> None:
        del trace
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
        for start in range(0, len(ids), 500):
            self.collection.delete(ids=ids[start : start + 500])

    def _read_records(self, *, include_metadata: bool = True) -> list[dict[str, Any]]:
        """Read Chroma's SQLite metadata segment without invoking the native HNSW binding.

        Chroma 1.x on Windows can terminate the interpreter while counting some persisted
        collections. Management views do not need embeddings, so using the documented local
        persistence database keeps these reads deterministic and prevents a native crash.
        """
        database_path = self.persist_path / "chroma.sqlite3"
        if not database_path.is_file():
            return []
        uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as connection:
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
