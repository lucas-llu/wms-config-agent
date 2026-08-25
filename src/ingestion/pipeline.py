"""Index preprocessed chunks into dense and sparse retrieval stores."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.trace import TraceCollector
from core.types import Chunk
from ingestion.corpus_manifest import CorpusManifestBuilder, CorpusManifestEntry
from ingestion.corpus_processor import CorpusProcessingReport, CorpusProcessor
from ingestion.embedding import BatchProcessor, DenseEncoder, SparseEncoder, SparseEncoding
from ingestion.storage import BM25Indexer, LifecycleLock, VectorUpserter
from libs.embedding import BaseEmbedding
from libs.vector_store import BaseVectorStore

IndexingProgressCallback = Callable[[str, int, int], None]
IngestionProgressCallback = Callable[[str, int, int], None]


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


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Paths and storage counts needed by Dashboard document lifecycle operations."""

    document_id: str
    collection: str
    source_path: str
    staged_pdf_path: str
    document_artifact_path: str
    chunk_artifact_paths: tuple[str, ...]
    processing: CorpusProcessingReport
    indexing: IndexingReport
    skipped: bool
    trace_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        values = asdict(self)
        values["processing"] = self.processing.to_dict()
        values["indexing"] = self.indexing.to_dict()
        return values


def load_preprocessed_chunks(path: str | Path | tuple[Path, ...]) -> list[Chunk]:
    """Load deterministic Chunk contracts from preprocessing JSONL files."""
    if isinstance(path, str | Path):
        chunks_path = Path(path)
        if not chunks_path.is_dir():
            raise FileNotFoundError(f"Preprocessed chunks directory does not exist: {chunks_path}")
        jsonl_paths = sorted(chunks_path.glob("*.jsonl"))
        source_description = str(chunks_path)
    else:
        jsonl_paths = sorted((Path(value) for value in path), key=lambda item: item.as_posix())
        missing = [item for item in jsonl_paths if not item.is_file()]
        if missing:
            raise FileNotFoundError(f"Preprocessed chunk artifact does not exist: {missing[0]}")
        source_description = ", ".join(item.as_posix() for item in jsonl_paths)

    chunks: list[Chunk] = []
    seen_ids: set[str] = set()
    for jsonl_path in jsonl_paths:
        for _line_number, line in enumerate(
            jsonl_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                chunk = Chunk(**payload)
            except (json.JSONDecodeError, TypeError, ValueError):
                # Legacy directory scans may surface partial or corrupt files left by
                # a crashed process.  Downstream selection filters out any chunk not
                # present in Dense/BM25 indexes, so skipping unparseable entries here
                # prevents restart failures without resurrecting orphans.
                continue
            if chunk.id in seen_ids:
                raise ValueError(f"Duplicate chunk id in preprocessed corpus: {chunk.id}")
            seen_ids.add(chunk.id)
            chunks.append(chunk)

    if not chunks:
        raise ValueError(f"No preprocessed chunks found in: {source_description}")
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
        finalize: Callable[[], None] | None = None,
    ) -> IndexingReport:
        if not chunks:
            raise ValueError("chunks must not be empty")
        refresh_generation = getattr(self.vector_store, "refresh_active_generation", None)
        if callable(refresh_generation):
            refresh_generation()
        ordered_chunks = sorted(chunks, key=lambda chunk: chunk.id)
        dense_deleted = 0
        model_prepared = False
        sparse_attempted = False
        sparse_snapshot = self._snapshot_sparse_index()
        validated_counts: dict[str, int] = {}
        active_stage = "embedding_fit"
        embed_started = time.perf_counter()
        started = time.perf_counter()
        try:
            prepare_fit = getattr(self.embedding, "prepare_fit", None)
            if callable(prepare_fit):
                model_trained = bool(
                    prepare_fit(
                        [DenseEncoder.embedding_text(chunk) for chunk in ordered_chunks],
                        force=force,
                    )
                )
                model_prepared = model_trained
            else:
                model_trained = self.embedding.fit(
                    [DenseEncoder.embedding_text(chunk) for chunk in ordered_chunks],
                    force=force,
                )
            self._record_stage(
                trace,
                "embedding_fit",
                started,
                {
                    "method": type(self.embedding).__name__,
                    "provider": type(self.embedding).__module__,
                    "chunk_count": len(ordered_chunks),
                    "model_trained": model_trained,
                    "transactional": callable(prepare_fit),
                },
            )
            signature = self.embedding.signature
            if on_progress:
                on_progress("fit", len(ordered_chunks), len(ordered_chunks))

            pending = (
                ordered_chunks
                if force or model_trained
                else self._changed_or_missing_chunks(ordered_chunks, signature)
            )
            existing_ids = self._existing_ids()
            current_ids = {chunk.id for chunk in ordered_chunks}
            stale_ids = sorted((existing_ids or set()) - current_ids)
            atomic_replace = getattr(self.vector_store, "replace_all_atomically", None)
            needs_dense_sync = bool(pending or stale_ids or force or model_trained)
            if finalize is not None and needs_dense_sync and not callable(atomic_replace):
                active_stage = "vector_capability"
                raise RuntimeError(
                    "Coordinated ingestion requires a vector store with atomic full-corpus "
                    "replacement support"
                )
            encoded_chunks = (
                ordered_chunks if callable(atomic_replace) and needs_dense_sync else pending
            )

            active_stage = "dense_encode"
            started = time.perf_counter()
            records = BatchProcessor(
                DenseEncoder(self.embedding), batch_size=self.batch_size
            ).encode(
                encoded_chunks,
                on_progress=(
                    (lambda current, total: on_progress("dense_encode", current, total))
                    if on_progress
                    else None
                ),
            )
            if on_progress and not encoded_chunks:
                on_progress("dense_encode", 0, 0)
            self._record_stage(
                trace,
                "dense_encode",
                started,
                {
                    "method": type(self.embedding).__name__,
                    "provider": type(self.embedding).__module__,
                    "record_count": len(records),
                },
            )
            active_stage = "sparse_encode"
            sparse_encodings = SparseEncoder().encode(ordered_chunks)
            self._record_stage(
                trace,
                "embed",
                embed_started,
                {
                    "method": type(self.embedding).__name__,
                    "provider": type(self.embedding).__module__,
                    "chunk_count": len(ordered_chunks),
                    "dense_record_count": len(records),
                    "sparse_record_count": len(sparse_encodings),
                    "batch_count": math.ceil(len(encoded_chunks) / self.batch_size),
                    "dimensions": getattr(self.embedding, "actual_dimensions", None),
                },
            )

            upsert_started = time.perf_counter()
            vector_started = time.perf_counter()

            def commit_sparse_and_model() -> None:
                nonlocal active_stage, sparse_attempted
                self._record_stage(
                    trace,
                    "vector_upsert",
                    vector_started,
                    {
                        "method": type(self.vector_store).__name__,
                        "provider": type(self.vector_store).__module__,
                        "record_count": len(records),
                        "mode": "atomic_replace",
                    },
                )
                if on_progress:
                    on_progress("vector_upsert", len(records), len(encoded_chunks))
                active_stage = "bm25_build"
                sparse_attempted = True
                sparse_started = time.perf_counter()
                self.bm25_indexer.build(sparse_encodings)
                self._record_stage(
                    trace,
                    "bm25_build",
                    sparse_started,
                    {
                        "method": type(self.bm25_indexer).__name__,
                        "provider": type(self.bm25_indexer).__module__,
                        "record_count": len(sparse_encodings),
                    },
                )
                if on_progress:
                    on_progress("bm25_build", len(ordered_chunks), len(ordered_chunks))
                active_stage = "embedding_commit"
                if model_prepared:
                    self.embedding.commit_fit()
                active_stage = "history_commit"
                if finalize is not None:
                    finalize()
                active_stage = "commit_validation"
                self._validate_committed_counts(ordered_chunks, validated_counts)
                active_stage = "vector_upsert"

            if callable(atomic_replace) and needs_dense_sync:
                active_stage = "vector_upsert"
                dense_deleted = len(stale_ids)
                atomic_replace(records, finalize=commit_sparse_and_model)
            else:
                active_stage = "vector_upsert"
                VectorUpserter(self.vector_store).upsert(records)
                self._record_stage(
                    trace,
                    "vector_upsert",
                    vector_started,
                    {
                        "method": type(self.vector_store).__name__,
                        "provider": type(self.vector_store).__module__,
                        "record_count": len(records),
                        "mode": "incremental",
                    },
                )
                if on_progress:
                    on_progress("vector_upsert", len(records), len(encoded_chunks))
                if stale_ids:
                    dense_deleted = self._delete_stale_chunks(ordered_chunks, trace)
                active_stage = "bm25_build"
                sparse_attempted = True
                sparse_started = time.perf_counter()
                self.bm25_indexer.build(sparse_encodings)
                self._record_stage(
                    trace,
                    "bm25_build",
                    sparse_started,
                    {
                        "method": type(self.bm25_indexer).__name__,
                        "provider": type(self.bm25_indexer).__module__,
                        "record_count": len(sparse_encodings),
                    },
                )
                if on_progress:
                    on_progress("bm25_build", len(ordered_chunks), len(ordered_chunks))
                active_stage = "embedding_commit"
                if model_prepared:
                    self.embedding.commit_fit()
                active_stage = "history_commit"
                if finalize is not None:
                    finalize()
                active_stage = "commit_validation"
                self._validate_committed_counts(ordered_chunks, validated_counts)

            active_stage = "embedding_finalize"
            if model_prepared:
                with suppress(Exception):
                    self.embedding.finalize_fit()
                    # Model bytes and every query-facing store are already committed;
                    # retaining rollback state is safer than reversing business success.

            with suppress(Exception):
                self._record_stage(
                    trace,
                    "upsert",
                    upsert_started,
                    {
                        "method": [
                            type(self.vector_store).__name__,
                            type(self.bm25_indexer).__name__,
                        ],
                        "provider": [
                            type(self.vector_store).__module__,
                            type(self.bm25_indexer).__module__,
                        ],
                        "dense_upserted": len(records),
                        "bm25_upserted": len(sparse_encodings),
                        "dense_deleted": dense_deleted,
                    },
                )
        except Exception as exc:
            rollback: dict[str, str] = {}
            if sparse_attempted:
                try:
                    self.bm25_indexer.build(sparse_snapshot)
                except Exception as rollback_exc:
                    rollback["bm25"] = f"failed:{type(rollback_exc).__name__}"
                else:
                    rollback["bm25"] = "restored"
            if model_prepared:
                try:
                    self.embedding.rollback_fit()
                except Exception as rollback_exc:
                    rollback["embedding"] = f"failed:{type(rollback_exc).__name__}"
                else:
                    rollback["embedding"] = "restored"
            self._record_stage(
                trace,
                f"{active_stage}_failure",
                started,
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "rollback": rollback,
                },
            )
            raise
        return IndexingReport(
            total_chunks=len(ordered_chunks),
            model_trained=model_trained,
            dense_upserted=len(records),
            dense_skipped=len(ordered_chunks) - len(records),
            dense_deleted=dense_deleted,
            vector_count=validated_counts["vector_count"],
            bm25_count=validated_counts["bm25_count"],
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

    def _changed_or_missing_chunks(self, chunks: list[Chunk], signature: str) -> list[Chunk]:
        existing: dict[str, dict[str, object]] = {}
        for start in range(0, len(chunks), 500):
            ids = [chunk.id for chunk in chunks[start : start + 500]]
            existing.update({record["id"]: record for record in self.vector_store.get_by_ids(ids)})

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

    def _existing_ids(self) -> set[str] | None:
        try:
            return set(self.vector_store.list_ids())
        except NotImplementedError:
            return None

    def _snapshot_sparse_index(self) -> list[SparseEncoding]:
        self.bm25_indexer.load()
        return [
            SparseEncoding(
                chunk_id=chunk_id,
                term_frequencies={
                    str(term): int(frequency)
                    for term, frequency in document.get("term_frequencies", {}).items()
                },
                document_length=int(document.get("length", 0)),
                metadata=(
                    dict(document["metadata"]) if isinstance(document.get("metadata"), dict) else {}
                ),
            )
            for chunk_id, document in self.bm25_indexer.documents.items()
        ]

    def _validate_committed_counts(
        self,
        chunks: list[Chunk],
        destination: dict[str, int],
    ) -> None:
        vector_count = self.vector_store.count()
        bm25_count = self.bm25_indexer.count()
        expected = len(chunks)
        if vector_count != expected or bm25_count != expected:
            raise RuntimeError(
                "Committed retrieval store counts do not match the full corpus snapshot"
            )
        destination["vector_count"] = vector_count
        destination["bm25_count"] = bm25_count

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


class IngestionPipeline:
    """Preprocess one staged PDF, then synchronize the complete local retrieval corpus."""

    def __init__(
        self,
        *,
        corpus_processor: CorpusProcessor,
        indexing_pipeline: IndexingPipeline,
        trace_collector: TraceCollector | None = None,
        manifest_builder: CorpusManifestBuilder | None = None,
        lifecycle_lock: LifecycleLock | None = None,
    ) -> None:
        self.corpus_processor = corpus_processor
        self.indexing_pipeline = indexing_pipeline
        self.trace_collector = trace_collector
        self.manifest_builder = manifest_builder or CorpusManifestBuilder()
        self.lifecycle_lock = lifecycle_lock or LifecycleLock.for_database(
            self.corpus_processor.integrity.database_path
        )

    def run(
        self,
        source_path: str | Path,
        collection: str = "default",
        on_progress: IngestionProgressCallback | None = None,
        *,
        trace: Any | None = None,
        force: bool = False,
    ) -> IngestionResult:
        path = Path(source_path).resolve()
        if not isinstance(collection, str) or not collection.strip():
            raise ValueError("collection must be a non-empty string")
        collection = collection.strip()
        entry = self.manifest_builder.build_entry(
            path,
            source_root=self.corpus_processor.source_root,
        )
        owned_trace = trace is None and self.trace_collector is not None
        if owned_trace:
            trace = self.trace_collector.start(
                "ingestion",
                {
                    "source_name": path.name,
                    "collection": collection,
                },
            )

        try:
            with self.lifecycle_lock.lease():
                (
                    processing,
                    indexing,
                    document_artifact,
                    chunks_artifact,
                ) = self._run_locked(
                    path=path,
                    entry=entry,
                    collection=collection,
                    on_progress=on_progress,
                    trace=trace,
                    force=force,
                )
                document_id = document_artifact.stem
            if owned_trace and trace is not None:
                trace.finish()
            return IngestionResult(
                document_id=document_id,
                collection=collection,
                source_path=entry.source_path,
                staged_pdf_path=str(path),
                document_artifact_path=str(document_artifact.resolve()),
                chunk_artifact_paths=(str(chunks_artifact.resolve()),),
                processing=processing,
                indexing=indexing,
                skipped=processing.skipped == 1,
                trace_id=getattr(trace, "trace_id", None),
            )
        except Exception as exc:
            if owned_trace and trace is not None:
                trace.finish(status="error", error=type(exc).__name__)
            raise
        finally:
            if owned_trace and self.trace_collector is not None:
                self.trace_collector.collect(trace)

    def _run_locked(
        self,
        *,
        path: Path,
        entry: CorpusManifestEntry,
        collection: str,
        on_progress: IngestionProgressCallback | None,
        trace: Any | None,
        force: bool,
    ) -> tuple[CorpusProcessingReport, IndexingReport, Path, Path]:
        """Run and roll back while the shared lifecycle lock remains held."""

        self._verify_source_hash(path, entry.file_hash)
        artifact_backups = self.corpus_processor.capture_artifacts(
            entry,
            collection=collection,
        )
        history_snapshot = self.corpus_processor.capture_history(
            entry,
            collection=collection,
        )
        image_inventory = self.corpus_processor.capture_extracted_image_inventory()
        processing_completed = False
        try:
            processing = self.corpus_processor.process(
                [entry],
                force=force,
                fail_fast=True,
                defer_success=True,
                collection=collection,
                on_progress=on_progress,
                trace=trace,
            )
            processing_completed = True
            self._verify_source_hash(path, entry.file_hash)
            chunks = load_preprocessed_chunks(
                self.corpus_processor.indexable_chunk_paths(
                    entry,
                    collection=collection,
                )
            )
            chunks = self._select_indexed_or_current_chunks(
                chunks,
                entry=entry,
                collection=collection,
            )
            self._validate_resync_snapshot(chunks, entry=entry, collection=collection)
            commit_values: dict[str, int] = {}

            def commit_history() -> None:
                commit_started = time.perf_counter()
                self._verify_source_hash(path, entry.file_hash)
                superseded_count = self.corpus_processor.finalize_indexing(
                    entry,
                    collection=collection,
                )
                commit_values["superseded_count"] = superseded_count
                self._record_stage(
                    trace,
                    "history_commit",
                    commit_started,
                    {
                        "method": type(self.corpus_processor.integrity).__name__,
                        "provider": type(self.corpus_processor.integrity).__module__,
                        "superseded_count": superseded_count,
                    },
                )

            indexing = self.indexing_pipeline.index(
                chunks,
                force=force,
                on_progress=(
                    self._index_progress_adapter(on_progress) if on_progress is not None else None
                ),
                trace=trace,
                finalize=commit_history,
            )
            cleanup_started = time.perf_counter()
            cleanup: dict[str, object]
            try:
                if processing.skipped == 0:
                    self.corpus_processor.finalize_optional_assets(
                        entry,
                        collection=collection,
                    )
                cleanup = self.corpus_processor.cleanup_superseded(
                    entry,
                    collection=collection,
                )
            except Exception as cleanup_exc:
                cleanup = {
                    "target_count": commit_values.get("superseded_count", 0),
                    "removed_count": 0,
                    "status": "partial",
                    "error_types": (type(cleanup_exc).__name__,),
                }
            self._record_stage(
                trace,
                "superseded_cleanup",
                cleanup_started,
                {
                    "method": "best_effort_lifecycle_cleanup",
                    "provider": type(self.corpus_processor).__module__,
                    **cleanup,
                },
            )
            document_artifact, chunks_artifact = self.corpus_processor.artifact_paths(
                entry.document_id,
                collection=collection,
            )
            return processing, indexing, document_artifact, chunks_artifact
        except Exception as exc:
            rollback_details: dict[str, str] = {}
            try:
                self.corpus_processor.restore_artifacts(artifact_backups)
            except Exception as rollback_exc:
                rollback_details["artifacts"] = f"failed:{type(rollback_exc).__name__}"
            else:
                rollback_details["artifacts"] = "restored"
            try:
                removed_images = self.corpus_processor.remove_new_extracted_images(image_inventory)
            except Exception as rollback_exc:
                rollback_details["extracted_images"] = f"failed:{type(rollback_exc).__name__}"
            else:
                rollback_details["extracted_images"] = f"removed:{removed_images}"
            if processing_completed:
                try:
                    self.corpus_processor.restore_history(
                        entry,
                        history_snapshot,
                        collection=collection,
                    )
                except Exception as rollback_exc:
                    rollback_details["history"] = f"failed:{type(rollback_exc).__name__}"
                else:
                    rollback_details["history"] = "restored"
            self._record_stage(
                trace,
                "ingestion_rollback",
                time.perf_counter(),
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "rollback": rollback_details,
                },
            )
            raise

    def _select_indexed_or_current_chunks(
        self,
        chunks: list[Chunk],
        *,
        entry: CorpusManifestEntry,
        collection: str,
    ) -> list[Chunk]:
        """Exclude legacy disk orphans while retaining indexed and current chunks."""

        try:
            dense_ids = set(self.indexing_pipeline.vector_store.list_ids())
        except NotImplementedError:
            dense_ids = set()
        self.indexing_pipeline.bm25_indexer.load()
        indexed_ids = dense_ids | set(self.indexing_pipeline.bm25_indexer.documents)
        current_filtered = [
            chunk
            for chunk in chunks
            if not self._is_current_source(chunk.metadata, entry, collection)
            or chunk.metadata.get("file_hash") == entry.file_hash
        ]
        if not indexed_ids:
            return current_filtered
        selected: list[Chunk] = []
        for chunk in current_filtered:
            if self._is_current_source(chunk.metadata, entry, collection):
                if chunk.metadata.get("file_hash") == entry.file_hash:
                    selected.append(chunk)
                continue
            if chunk.id in indexed_ids:
                selected.append(chunk)
        if not selected:
            raise RuntimeError("No committed or current chunks remain after corpus validation")
        return selected

    def _validate_resync_snapshot(
        self,
        chunks: list[Chunk],
        *,
        entry: CorpusManifestEntry,
        collection: str,
    ) -> None:
        """Fail closed before a full replacement can drop unrelated indexed chunks."""

        desired_ids = {chunk.id for chunk in chunks}
        try:
            dense_ids = set(self.indexing_pipeline.vector_store.list_ids())
        except NotImplementedError:
            dense_ids = set()
        missing_dense = sorted(dense_ids - desired_ids)
        if missing_dense:
            records = self.indexing_pipeline.vector_store.get_by_ids(missing_dense)
            if len(records) != len(missing_dense) or any(
                not self._is_current_source(record.get("metadata"), entry, collection)
                for record in records
            ):
                raise RuntimeError(
                    "Refusing corpus resync because processed artifacts do not cover "
                    "the existing Dense index"
                )

        self.indexing_pipeline.bm25_indexer.load()
        missing_sparse = set(self.indexing_pipeline.bm25_indexer.documents) - desired_ids
        if missing_sparse and any(
            not self._is_current_source(
                self.indexing_pipeline.bm25_indexer.documents[chunk_id].get("metadata"),
                entry,
                collection,
            )
            for chunk_id in missing_sparse
        ):
            raise RuntimeError(
                "Refusing corpus resync because processed artifacts do not cover "
                "the existing BM25 index"
            )

    @staticmethod
    def _is_current_source(
        metadata: object,
        entry: CorpusManifestEntry,
        collection: str,
    ) -> bool:
        if not isinstance(metadata, dict):
            return False
        source = metadata.get("source_relative_path") or metadata.get("source_path")
        return bool(
            metadata.get("collection") == collection
            and isinstance(source, str)
            and CorpusProcessor._path_key(source) == CorpusProcessor._path_key(entry.source_path)
        )

    def _verify_source_hash(self, path: Path, expected_hash: str) -> None:
        actual_hash = self.corpus_processor.integrity.compute_sha256(path)
        if actual_hash != expected_hash:
            raise RuntimeError("Source changed during ingestion; retry with a fresh manifest entry")

    @staticmethod
    def _record_stage(
        trace: Any | None,
        name: str,
        started: float,
        details: dict[str, Any],
    ) -> None:
        if trace is not None and hasattr(trace, "record_stage"):
            trace.record_stage(name, (time.perf_counter() - started) * 1000, details=details)

    @staticmethod
    def _index_progress_adapter(
        callback: IngestionProgressCallback,
    ) -> IndexingProgressCallback:
        def report(stage: str, current: int, total: int) -> None:
            if stage == "dense_encode":
                callback("embed", current if total else 1, max(total, 1))
            elif stage == "vector_upsert":
                callback("upsert", 1, 2)
            elif stage == "bm25_build":
                callback("upsert", 2, 2)

        return report
