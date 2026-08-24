"""Persistent Okapi BM25 inverted index."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from ingestion.embedding import SparseEncoder, SparseEncoding


class BM25Indexer:
    def __init__(
        self,
        persist_path: str | Path = "data/db/bm25",
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.persist_path = Path(persist_path)
        self.index_path = self.persist_path / "index.json"
        self.k1 = k1
        self.b = b
        self.documents: dict[str, dict[str, Any]] = {}
        self.postings: dict[str, dict[str, int]] = {}
        self.average_document_length = 0.0
        if self.index_path.is_file():
            self.load()

    def build(self, encodings: list[SparseEncoding]) -> None:
        self.documents = {
            encoding.chunk_id: {
                "length": encoding.document_length,
                "term_frequencies": encoding.term_frequencies,
            }
            for encoding in encodings
        }
        self._rebuild_postings()
        self.save()

    def upsert(self, encodings: list[SparseEncoding]) -> None:
        for encoding in encodings:
            self.documents[encoding.chunk_id] = {
                "length": encoding.document_length,
                "term_frequencies": encoding.term_frequencies,
            }
        self._rebuild_postings()
        self.save()

    def query(self, query: str | list[str], top_k: int = 10) -> list[dict[str, Any]]:
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0")
        terms = SparseEncoder.tokenize(query) if isinstance(query, str) else query
        if not terms or not self.documents:
            return []
        scores: dict[str, float] = {}
        document_count = len(self.documents)
        for term in (value.lower() for value in terms):
            term_postings = self.postings.get(term)
            if not term_postings:
                continue
            document_frequency = len(term_postings)
            idf = math.log(
                1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for chunk_id, frequency in term_postings.items():
                document_length = int(self.documents[chunk_id]["length"])
                denominator = frequency + self.k1 * (
                    1.0 - self.b + self.b * document_length / max(self.average_document_length, 1.0)
                )
                score = idf * (frequency * (self.k1 + 1.0)) / denominator
                scores[chunk_id] = scores.get(chunk_id, 0.0) + score
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return [{"chunk_id": chunk_id, "score": score} for chunk_id, score in ranked]

    def save(self) -> None:
        self.persist_path.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "k1": self.k1,
                    "b": self.b,
                    "average_document_length": self.average_document_length,
                    "documents": self.documents,
                    "postings": self.postings,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.index_path)

    def load(self) -> None:
        values = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.k1 = float(values["k1"])
        self.b = float(values["b"])
        self.average_document_length = float(values["average_document_length"])
        self.documents = values["documents"]
        self.postings = values["postings"]

    def count(self) -> int:
        return len(self.documents)

    def remove_document(self, chunk_ids: list[str]) -> int:
        """Remove all supplied chunk IDs and persist a rebuilt index."""
        removed = 0
        for chunk_id in set(chunk_ids):
            if self.documents.pop(chunk_id, None) is not None:
                removed += 1
        if removed:
            self._rebuild_postings()
            self.save()
        return removed

    def _rebuild_postings(self) -> None:
        self.postings = {}
        total_length = 0
        for chunk_id, document in self.documents.items():
            total_length += int(document["length"])
            for term, frequency in document["term_frequencies"].items():
                self.postings.setdefault(term, {})[chunk_id] = int(frequency)
        self.average_document_length = total_length / max(len(self.documents), 1)
