"""Dense encoding adapter from Chunk to ChunkRecord."""

from __future__ import annotations

import hashlib

from core.types import Chunk, ChunkRecord
from libs.embedding import BaseEmbedding


class DenseEncoder:
    def __init__(self, embedding: BaseEmbedding) -> None:
        self.embedding = embedding

    def encode(self, chunks: list[Chunk]) -> list[ChunkRecord]:
        if not chunks:
            return []
        vectors = self.embedding.embed([self.embedding_text(chunk) for chunk in chunks])
        if len(vectors) != len(chunks):
            raise RuntimeError("Embedding provider returned a different number of vectors")
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1 or 0 in dimensions:
            raise RuntimeError("Embedding provider returned inconsistent vector dimensions")

        records: list[ChunkRecord] = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            metadata = dict(chunk.metadata)
            metadata["embedding_signature"] = self.embedding.signature
            metadata["content_hash"] = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            records.append(
                ChunkRecord(
                    id=chunk.id,
                    text=chunk.text,
                    metadata=metadata,
                    dense_vector=vector,
                )
            )
        return records

    @staticmethod
    def embedding_text(chunk: Chunk) -> str:
        """Add searchable business context without changing the cited source text."""
        tags = chunk.metadata.get("tags")
        searchable_tags = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else tags
        context_fields = (
            ("Title", chunk.metadata.get("title")),
            ("Summary", chunk.metadata.get("summary")),
            ("Tags", searchable_tags),
            ("Process code", chunk.metadata.get("process_code")),
            ("Process stage", chunk.metadata.get("process_stage")),
            ("Domain", chunk.metadata.get("domain")),
            ("Document type", chunk.metadata.get("document_type")),
        )
        context = "\n".join(f"{label}: {value}" for label, value in context_fields if value)
        return f"{context}\n\n{chunk.text}" if context else chunk.text
