"""Persistent Chroma vector store using caller-provided embeddings."""

from __future__ import annotations

import json
import math
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
        result = self.collection.get(
            ids=ids,
            include=["documents", "metadatas"],
        )
        records = {
            chunk_id: {
                "id": chunk_id,
                "text": document or "",
                "metadata": self._restore_metadata(metadata or {}),
            }
            for chunk_id, document, metadata in zip(
                result.get("ids") or [],
                result.get("documents") or [],
                result.get("metadatas") or [],
                strict=True,
            )
        }
        return [records[chunk_id] for chunk_id in ids if chunk_id in records]

    def count(self) -> int:
        return self.collection.count()

    def list_ids(self) -> list[str]:
        ids: list[str] = []
        offset = 0
        while True:
            result = self.collection.get(limit=1000, offset=offset, include=[])
            batch = [str(value) for value in (result.get("ids") or [])]
            if not batch:
                return ids
            ids.extend(batch)
            offset += len(batch)

    def delete(self, ids: list[str]) -> None:
        for start in range(0, len(ids), 500):
            self.collection.delete(ids=ids[start : start + 500])

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
            restored = json.loads(serialized)
        else:
            restored = {key: value for key, value in metadata.items() if key != _FULL_METADATA_KEY}
        for key in ("embedding_signature", "content_hash"):
            if key in metadata:
                restored[key] = metadata[key]
        return restored
