"""Token statistics used to build the local BM25 index."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from core.types import Chunk
from ingestion.embedding.dense_encoder import DenseEncoder

_TOKEN = re.compile(r"[A-Za-z0-9_.$-]{2,}|[\u4e00-\u9fff]")


@dataclass(frozen=True, slots=True)
class SparseEncoding:
    chunk_id: str
    term_frequencies: dict[str, int]
    document_length: int


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
                )
            )
        return encodings

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return [token.lower() for token in _TOKEN.findall(text)]
