"""Index preprocessed chunks into dense and sparse retrieval stores."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.types import Chunk
from ingestion.embedding import BatchProcessor, DenseEncoder, SparseEncoder
from ingestion.storage import BM25Indexer, VectorUpserter
from libs.embedding import BaseEmbedding
from libs.vector_store import BaseVectorStore

IndexingProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class IndexingReport:
    total_chunks: int
    model_trained: bool
    dense_upserted: int
    dense_skipped: int
    dense_deleted: int
    vector_count: int
    bm25_count: int
    embedding_signature: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_preprocessed_chunks(path: str | Path) -> list[Chunk]:
    """Load deterministic Chunk contracts from preprocessing JSONL files."""
    chunks_path = Path(path)
    if not chunks_path.is_dir():
        raise FileNotFoundError(f"Preprocessed chunks directory does not exist: {chunks_path}")

    chunks: list[Chunk] = []
    seen_ids: set[str] = set()
    for jsonl_path in sorted(chunks_path.glob("*.jsonl")):
        for line_number, line in enumerate(
            jsonl_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                chunk = Chunk(**payload)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid chunk at {jsonl_path}:{line_number}: {exc}"
                ) from exc
            if chunk.id in seen_ids:
                raise ValueError(f"Duplicate chunk id in preprocessed corpus: {chunk.id}")
            seen_ids.add(chunk.id)
            chunks.append(chunk)

    if not chunks:
        raise ValueError(f"No preprocessed chunks found in: {chunks_path}")
    return sorted(chunks, key=lambda chunk: chunk.id)


class IndexingPipeline:
    """Fit embeddings and persist idempotent dense and BM25 indexes."""

    def __init__(
        self,
        *,
        embedding: BaseEmbedding,
        vector_store: BaseVectorStore,
        bm25_indexer: BM25Indexer,
        batch_size: int = 32,
    ) -> None:
        self.embedding = embedding
        self.vector_store = vector_store
        self.bm25_indexer = bm25_indexer
        self.batch_size = batch_size

    def index(
        self,
        chunks: list[Chunk],
        *,
        force: bool = False,
        on_progress: IndexingProgressCallback | None = None,
        trace: Any | None = None,
    ) -> IndexingReport:
        if not chunks:
            raise ValueError("chunks must not be empty")
        ordered_chunks = sorted(chunks, key=lambda chunk: chunk.id)
        dense_deleted = 0
        started = time.perf_counter()
        model_trained = self.embedding.fit(
            [DenseEncoder.embedding_text(chunk) for chunk in ordered_chunks], force=force
        )
        self._record_stage(
            trace,
            "embedding_fit",
            started,
            {"chunk_count": len(ordered_chunks), "model_trained": model_trained},
        )
        signature = self.embedding.signature
        if on_progress:
            on_progress("fit", len(ordered_chunks), len(ordered_chunks))

        pending = (
            ordered_chunks
            if force or model_trained
            else self._changed_or_missing_chunks(ordered_chunks, signature)
        )
        started = time.perf_counter()
        records = BatchProcessor(
            DenseEncoder(self.embedding), batch_size=self.batch_size
        ).encode(
            pending,
            on_progress=(
                (lambda current, total: on_progress("dense_encode", current, total))
                if on_progress
                else None
            ),
        )
        self._record_stage(
            trace, "dense_encode", started, {"record_count": len(records)}
        )
        started = time.perf_counter()
        VectorUpserter(self.vector_store).upsert(records)
        self._record_stage(
            trace, "vector_upsert", started, {"record_count": len(records)}
        )
        if on_progress:
            on_progress("vector_upsert", len(records), len(pending))
        if force:
            dense_deleted = self._delete_stale_chunks(ordered_chunks, trace)

        started = time.perf_counter()
        sparse_encodings = SparseEncoder().encode(ordered_chunks)
        self.bm25_indexer.build(sparse_encodings)
        self._record_stage(
            trace, "bm25_build", started, {"record_count": len(sparse_encodings)}
        )
        if on_progress:
            on_progress("bm25_build", len(ordered_chunks), len(ordered_chunks))
        return IndexingReport(
            total_chunks=len(ordered_chunks),
            model_trained=model_trained,
            dense_upserted=len(records),
            dense_skipped=len(ordered_chunks) - len(records),
            dense_deleted=dense_deleted,
            vector_count=self.vector_store.count(),
            bm25_count=self.bm25_indexer.count(),
            embedding_signature=signature,
        )

    @staticmethod
    def _record_stage(
        trace: Any | None,
        name: str,
        started: float,
        details: dict[str, Any],
    ) -> None:
        if trace is not None and hasattr(trace, "record_stage"):
            trace.record_stage(name, (time.perf_counter() - started) * 1000, details=details)

    def _changed_or_missing_chunks(
        self, chunks: list[Chunk], signature: str
    ) -> list[Chunk]:
        existing: dict[str, dict[str, object]] = {}
        for start in range(0, len(chunks), 500):
            ids = [chunk.id for chunk in chunks[start : start + 500]]
            existing.update(
                {record["id"]: record for record in self.vector_store.get_by_ids(ids)}
            )

        pending: list[Chunk] = []
        for chunk in chunks:
            record = existing.get(chunk.id)
            metadata = record.get("metadata", {}) if record else {}
            content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            if not isinstance(metadata, dict) or (
                metadata.get("embedding_signature") != signature
                or metadata.get("content_hash") != content_hash
            ):
                pending.append(chunk)
        return pending

    def _delete_stale_chunks(self, chunks: list[Chunk], trace: Any | None) -> int:
        started = time.perf_counter()
        try:
            existing_ids = set(self.vector_store.list_ids())
        except NotImplementedError:
            return 0
        current_ids = {chunk.id for chunk in chunks}
        stale_ids = sorted(existing_ids - current_ids)
        if stale_ids:
            self.vector_store.delete(stale_ids)
        self._record_stage(
            trace,
            "vector_delete_stale",
            started,
            {"record_count": len(stale_ids)},
        )
        return len(stale_ids)
