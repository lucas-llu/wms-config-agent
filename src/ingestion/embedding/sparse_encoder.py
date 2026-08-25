"""Token statistics used to build the local BM25 index."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from core.types import Chunk
from ingestion.embedding.dense_encoder import DenseEncoder

_TOKEN = re.compile(r"[A-Za-z0-9_.$-]{2,}|[\u4e00-\u9fff]")


@dataclass(frozen=True, slots=True)
class SparseEncoding:
    chunk_id: str
    term_frequencies: dict[str, int]
    document_length: int
    metadata: dict[str, Any] = field(default_factory=dict)


class SparseEncoder:
    def encode(self, chunks: list[Chunk]) -> list[SparseEncoding]:
        encodings: list[SparseEncoding] = []
        for chunk in chunks:
            tokens = self.tokenize(DenseEncoder.embedding_text(chunk))
            encodings.append(
                SparseEncoding(
                    chunk_id=chunk.id,
                    term_frequencies=dict(Counter(tokens)),
                    document_length=len(tokens),
                    metadata=self._management_metadata(chunk.metadata),
                )
            )
        return encodings

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return [token.lower() for token in _TOKEN.findall(text)]

    @staticmethod
    def _management_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        """Keep only scalar fields needed to manage a sparse document lifecycle."""
        keys = (
            "collection",
            "document_id",
            "file_hash",
            "source_path",
            "source_relative_path",
        )
        return {
            key: metadata[key]
            for key in keys
            if key in metadata and isinstance(metadata[key], str | int | float | bool)
        }
