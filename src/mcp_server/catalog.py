"""Read-only catalog over private preprocessed chunk artifacts."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.types import Chunk
from ingestion import load_preprocessed_chunks


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    document_id: str
    title: str
    source: str
    collection: str
    domain: str | None
    process_code: str | None
    process_stage: str | None
    document_type: str | None
    page_count: int | None
    chunk_count: int
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CorpusCatalog:
    def __init__(self, chunks_path: str | Path) -> None:
        self.chunks_path = Path(chunks_path)
        self._chunks: list[Chunk] | None = None
        self._documents: list[DocumentSummary] | None = None

    def list_collections(self) -> list[dict[str, Any]]:
        chunks = self._load_chunks()
        documents = self._load_documents()
        document_counts: dict[str, int] = defaultdict(int)
        domains: dict[str, set[str]] = defaultdict(set)
        for document in documents:
            document_counts[document.collection] += 1
            if document.domain:
                domains[document.collection].add(document.domain)
        chunk_counts: dict[str, int] = defaultdict(int)
        for chunk in chunks:
            chunk_counts[str(chunk.metadata.get("collection", "default"))] += 1
        return [
            {
                "name": collection,
                "document_count": document_counts[collection],
                "chunk_count": chunk_counts[collection],
                "domains": sorted(domains[collection]),
            }
            for collection in sorted(set(document_counts) | set(chunk_counts))
        ]

    def find_documents(self, identifier: str) -> list[DocumentSummary]:
        normalized = identifier.strip().lower()
        if not normalized:
            raise ValueError("document_id must be a non-empty string")
        return [
            document
            for document in self._load_documents()
            if normalized
            in {
                document.document_id.lower(),
                document.source.lower(),
                (document.process_code or "").lower(),
            }
        ]

    def _load_chunks(self) -> list[Chunk]:
        if self._chunks is None:
            self._chunks = load_preprocessed_chunks(self.chunks_path)
        return self._chunks

    def _load_documents(self) -> list[DocumentSummary]:
        if self._documents is not None:
            return self._documents
        grouped: dict[str, list[Chunk]] = defaultdict(list)
        for chunk in self._load_chunks():
            document_id = str(
                chunk.source_ref or chunk.metadata.get("file_hash") or chunk.metadata["source_path"]
            )
            grouped[document_id].append(chunk)

        documents: list[DocumentSummary] = []
        for document_id, chunks in grouped.items():
            ordered = sorted(chunks, key=lambda chunk: chunk.start_offset)
            metadata = ordered[0].metadata
            excerpt = re.sub(r"\s+", " ", ordered[0].text).strip()[:500]
            documents.append(
                DocumentSummary(
                    document_id=document_id,
                    title=str(metadata.get("title") or metadata.get("source_name") or document_id),
                    source=str(metadata.get("source_relative_path") or metadata["source_path"]),
                    collection=str(metadata.get("collection", "default")),
                    domain=self._optional_str(metadata.get("domain")),
                    process_code=self._optional_str(metadata.get("process_code")),
                    process_stage=self._optional_str(metadata.get("process_stage")),
                    document_type=self._optional_str(metadata.get("document_type")),
                    page_count=self._optional_int(metadata.get("page_count")),
                    chunk_count=len(ordered),
                    excerpt=excerpt,
                )
            )
        self._documents = sorted(
            documents, key=lambda document: (document.source, document.document_id)
        )
        return self._documents

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return int(value) if isinstance(value, int | float) else None
