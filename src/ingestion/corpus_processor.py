"""Incrementally turn corpus manifest entries into private Document and Chunk files."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from core.settings import SplitterSettings
from core.types import Chunk, Document
from ingestion.chunking import DocumentChunker
from ingestion.corpus_manifest import CorpusManifestEntry
from ingestion.llm_failure_ledger import LLMFailureLedger, collect_llm_fallbacks
from ingestion.storage import ImageStorage
from ingestion.transform import BaseTransform
from libs.loader import BaseLoader, IngestionRecord, LoaderFactory, SQLiteIntegrityChecker

LoaderBuilder = Callable[..., BaseLoader]
CorpusProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class CorpusProcessingReport:
    total: int
    succeeded: int
    skipped: int
    duplicates: int
    failed: int
    chunks_written: int
    retried_documents: int
    retried_chunks: int
    remaining_llm_fallbacks: int
    errors: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class CorpusProcessor:
    """Persist local preprocessing artifacts with SHA256 incremental skipping."""

    def __init__(
        self,
        *,
        source_root: str | Path,
        output_root: str | Path,
        database_path: str | Path,
        splitter_settings: SplitterSettings,
        extract_images: bool = False,
        transforms: Sequence[BaseTransform] = (),
        image_storage: ImageStorage | None = None,
        image_collection: str = "wms-system-training",
        loader_builder: LoaderBuilder = LoaderFactory.create,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.output_root = Path(output_root)
        self.documents_dir = self.output_root / "documents"
        self.chunks_dir = self.output_root / "chunks"
        self.images_dir = self.output_root / "images"
        self.integrity = SQLiteIntegrityChecker(database_path)
        self.chunker = DocumentChunker(splitter_settings)
        self.extract_images = extract_images
        self.transforms = tuple(transforms)
        self.image_storage = image_storage
        self.image_collection = image_collection
        self.loader_builder = loader_builder
        self.processing_signature = self._processing_signature()

    def process(
        self,
        entries: list[CorpusManifestEntry],
        *,
        force: bool = False,
        fail_fast: bool = False,
        retry_llm_failures: bool = False,
        defer_success: bool = False,
        collection: str = "wms-system-training",
        on_progress: CorpusProgressCallback | None = None,
        trace: object | None = None,
    ) -> CorpusProcessingReport:
        collection = self._validate_collection(collection)
        processing_signature = self._collection_signature(collection)
        succeeded = 0
        skipped = 0
        duplicates = 0
        failed = 0
        chunks_written = 0
        retried_documents = 0
        retried_chunks = 0
        errors: list[dict[str, str]] = []
        failure_ledger = LLMFailureLedger(self.output_root / "llm_failures.jsonl")

        for entry in entries:
            if entry.duplicate_of is not None:
                duplicates += 1
                continue
            document_id = self._storage_document_id(entry.document_id, collection)
            document_output = self.documents_dir / f"{document_id}.json"
            chunks_output = self.chunks_dir / f"{document_id}.jsonl"
            can_skip = (
                not force
                and self._should_skip(entry.file_hash, collection, processing_signature)
                and document_output.is_file()
                and chunks_output.is_file()
            )
            if can_skip and retry_llm_failures:
                try:
                    chunks = self._read_chunks(chunks_output)
                    chunks, retry_count = self._retry_failed_chunks(chunks)
                    if retry_count:
                        fallbacks = collect_llm_fallbacks(
                            chunks,
                            document_id=document_id,
                            source_path=entry.source_path,
                        )
                        self._write_jsonl(chunks_output, [chunk.to_dict() for chunk in chunks])
                        failure_ledger.update_document(document_id, fallbacks)
                        if not defer_success:
                            self._mark_success(
                                entry,
                                collection=collection,
                                document_id=document_id,
                                document_output=document_output,
                                chunks_output=chunks_output,
                                chunk_count=len(chunks),
                                llm_fallback_count=len(fallbacks),
                                processing_signature=processing_signature,
                            )
                        succeeded += 1
                        chunks_written += len(chunks)
                        retried_documents += 1
                        retried_chunks += retry_count
                        continue
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    # A corrupt or temporarily unreadable artifact is rebuilt from source below.
                    can_skip = False
            if can_skip:
                try:
                    cached_chunks = self._read_chunks(chunks_output)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    can_skip = False
                else:
                    skipped += 1
                    if not defer_success:
                        self._mark_success(
                            entry,
                            collection=collection,
                            document_id=document_id,
                            document_output=document_output,
                            chunks_output=chunks_output,
                            chunk_count=len(cached_chunks),
                            llm_fallback_count=len(
                                collect_llm_fallbacks(
                                    cached_chunks,
                                    document_id=document_id,
                                    source_path=entry.source_path,
                                )
                            ),
                            processing_signature=processing_signature,
                        )
                    for stage in ("load", "split", "transform"):
                        started = time.perf_counter()
                        self._record_stage(
                            trace,
                            stage,
                            started,
                            {
                                "method": "cached_artifact",
                                "provider": type(self.integrity).__name__,
                                "skipped": True,
                                "chunk_count": len(cached_chunks),
                            },
                        )
                        self._emit_progress(on_progress, stage, 1, 1)
                    continue

            source_path = self.source_root / Path(entry.source_path)
            active_stage = "load"
            stage_started = time.perf_counter()
            try:
                started = stage_started
                loader = self.loader_builder(
                    source_path,
                    image_output_dir=self.images_dir,
                    extract_images=self.extract_images,
                )
                document = loader.load(source_path, self._domain_metadata(entry, collection))
                if document.id != document_id:
                    metadata = dict(document.metadata)
                    metadata["original_document_id"] = document.id
                    document = Document(id=document_id, text=document.text, metadata=metadata)
                if not defer_success:
                    self._store_document_images(document.metadata, collection=collection)
                self._record_stage(
                    trace,
                    "load",
                    started,
                    {
                        "method": type(loader).__name__,
                        "provider": type(loader).__module__,
                        "size_bytes": entry.size_bytes,
                        "image_count": len(document.metadata.get("images", [])),
                    },
                )
                self._emit_progress(on_progress, "load", 1, 1)

                active_stage = "split"
                stage_started = time.perf_counter()
                started = stage_started
                chunks = self.chunker.split_document(document)
                self._record_stage(
                    trace,
                    "split",
                    started,
                    {
                        "method": type(self.chunker.splitter).__name__,
                        "provider": type(self.chunker.splitter).__module__,
                        "chunk_count": len(chunks),
                        "average_chunk_length": round(
                            sum(len(chunk.text) for chunk in chunks) / max(len(chunks), 1), 2
                        ),
                    },
                )
                self._emit_progress(on_progress, "split", 1, 1)

                active_stage = "transform"
                stage_started = time.perf_counter()
                started = stage_started
                transform_total = max(len(self.transforms), 1)
                for index, transform in enumerate(self.transforms, start=1):
                    chunks = transform.transform(chunks, trace=trace)
                    self._emit_progress(on_progress, "transform", index, transform_total)
                if not self.transforms:
                    self._emit_progress(on_progress, "transform", 1, 1)
                self._record_stage(
                    trace,
                    "transform",
                    started,
                    {
                        "method": [type(item).__name__ for item in self.transforms],
                        "provider": [type(item).__module__ for item in self.transforms],
                        "transform_count": len(self.transforms),
                        "chunk_count": len(chunks),
                    },
                )
                fallbacks = collect_llm_fallbacks(
                    chunks,
                    document_id=document_id,
                    source_path=entry.source_path,
                )
                self._write_json(document_output, document.to_dict())
                self._write_jsonl(chunks_output, [chunk.to_dict() for chunk in chunks])
                failure_ledger.update_document(document_id, fallbacks)
                if not defer_success:
                    self._mark_success(
                        entry,
                        collection=collection,
                        document_id=document_id,
                        document_output=document_output,
                        chunks_output=chunks_output,
                        chunk_count=len(chunks),
                        llm_fallback_count=len(fallbacks),
                        processing_signature=processing_signature,
                    )
                succeeded += 1
                chunks_written += len(chunks)
            except Exception as exc:
                self._record_stage(
                    trace,
                    f"{active_stage}_failure",
                    stage_started,
                    {
                        "status": "error",
                        "error_type": type(exc).__name__,
                    },
                )
                failed += 1
                error = {
                    "source_path": entry.source_path,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
                errors.append(error)
                if not defer_success:
                    self._mark_failed(entry, collection, str(exc))
                if fail_fast:
                    raise

        failure_ledger.write()
        report = CorpusProcessingReport(
            total=len(entries),
            succeeded=succeeded,
            skipped=skipped,
            duplicates=duplicates,
            failed=failed,
            chunks_written=chunks_written,
            retried_documents=retried_documents,
            retried_chunks=retried_chunks,
            remaining_llm_fallbacks=failure_ledger.count,
            errors=tuple(errors),
        )
        self._write_json(self.output_root / "processing_report.json", report.to_dict())
        return report

    def artifact_paths(
        self, document_id: str, *, collection: str = "wms-system-training"
    ) -> tuple[Path, Path]:
        """Return deterministic processed artifact paths for one logical document."""
        stored_id = self._storage_document_id(document_id, self._validate_collection(collection))
        return (
            self.documents_dir / f"{stored_id}.json",
            self.chunks_dir / f"{stored_id}.jsonl",
        )

    def capture_artifacts(
        self,
        entry: CorpusManifestEntry,
        *,
        collection: str,
    ) -> dict[Path, bytes | None]:
        """Capture the current document artifacts so a failed resync can restore them."""

        document_path, chunks_path = self.artifact_paths(entry.document_id, collection=collection)
        paths = [document_path, chunks_path, self.output_root / "llm_failures.jsonl"]
        paths.extend(self._document_image_paths(document_path))
        return {path: path.read_bytes() if path.is_file() else None for path in paths}

    def restore_artifacts(self, backups: dict[Path, bytes | None]) -> None:
        """Restore pre-run artifacts, removing newly created uncommitted files."""

        document_paths = [path for path in backups if path.parent == self.documents_dir]
        current_images = {
            image_path
            for document_path in document_paths
            for image_path in self._document_image_paths(document_path)
        }
        for path, payload in backups.items():
            if payload is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".rollback.tmp")
            temporary.write_bytes(payload)
            self._replace_with_retry(temporary, path)
        for image_path in current_images - set(backups):
            if self._is_managed_extracted_image(image_path):
                image_path.unlink(missing_ok=True)

    def capture_history(
        self,
        entry: CorpusManifestEntry,
        *,
        collection: str,
    ) -> IngestionRecord | None:
        """Capture an exact logical history record before a coordinated mutation."""

        collection = self._validate_collection(collection)
        return next(
            (
                record
                for record in self.integrity.list_processed(
                    status=None,
                    collection=collection,
                )
                if record.file_hash == entry.file_hash
            ),
            None,
        )

    def capture_extracted_image_inventory(self) -> frozenset[Path]:
        """Snapshot managed raw image names without loading private image bytes."""

        if not self.images_dir.is_dir():
            return frozenset()
        return frozenset(path.resolve() for path in self.images_dir.rglob("*") if path.is_file())

    def remove_new_extracted_images(self, before: frozenset[Path]) -> int:
        """Remove raw image files created by an ingestion that did not commit."""

        if not self.images_dir.is_dir():
            return 0
        current = {path.resolve() for path in self.images_dir.rglob("*") if path.is_file()}
        removed = 0
        for path in current - set(before):
            if self._is_managed_extracted_image(path):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def restore_history(
        self,
        entry: CorpusManifestEntry,
        snapshot: IngestionRecord | None,
        *,
        collection: str,
    ) -> None:
        """Restore success/failed state if a coordinated storage commit rolls back."""

        collection = self._validate_collection(collection)
        self.integrity.remove_record(file_hash=entry.file_hash, collection=collection)
        if snapshot is None:
            return
        metadata = dict(snapshot.metadata)
        metadata["collection"] = collection
        if snapshot.status == "success":
            self.integrity.mark_success(
                snapshot.file_hash,
                snapshot.file_path,
                **metadata,
            )
        else:
            # Seed metadata through the success API; mark_failed preserves that payload.
            self.integrity.mark_success(
                snapshot.file_hash,
                snapshot.file_path,
                **metadata,
            )
            self.integrity.mark_failed(
                snapshot.file_hash,
                snapshot.error_msg or "restored ingestion failure",
                snapshot.file_path,
                collection=collection,
            )

    def _document_image_paths(self, document_path: Path) -> list[Path]:
        if not document_path.is_file():
            return []
        try:
            payload = json.loads(document_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return []
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return []
        tracked = metadata.get("extracted_image_artifact_paths")
        paths = (
            [
                Path(value).resolve()
                for value in tracked
                if isinstance(value, str) and self._is_managed_extracted_image(Path(value))
            ]
            if isinstance(tracked, list | tuple)
            else []
        )
        images = metadata.get("images")
        if not isinstance(images, list):
            return list(dict.fromkeys(paths))
        paths.extend(
            Path(value["path"]).resolve()
            for value in images
            if isinstance(value, dict)
            and isinstance(value.get("path"), str)
            and self._is_managed_extracted_image(Path(value["path"]))
        )
        return list(dict.fromkeys(paths))

    def _is_managed_extracted_image(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.images_dir.resolve())
        except ValueError:
            return False
        return True

    def indexable_chunk_paths(
        self,
        entry: CorpusManifestEntry,
        *,
        collection: str,
    ) -> tuple[Path, ...]:
        """Return the committed corpus plus the current deferred ingestion artifact.

        Reading only successful history records prevents another process's prepared but
        uncommitted artifact from leaking into this run's full-corpus snapshot.
        """

        collection = self._validate_collection(collection)
        superseded = {
            (record.file_hash, record.collection or "")
            for record in self._superseded_records(entry, collection=collection)
        }
        superseded_paths: set[Path] = set()
        paths: set[Path] = set()
        records = self.integrity.list_processed(status="success")
        for record in records:
            derived_path = self._derived_chunk_artifact(record)
            if (record.file_hash, record.collection or "") in superseded:
                if derived_path is not None:
                    superseded_paths.add(derived_path.resolve())
                continue
            artifact_paths = record.metadata.get("chunk_artifact_paths")
            if not isinstance(artifact_paths, list | tuple):
                if derived_path is not None and derived_path.is_file():
                    paths.add(derived_path.resolve())
                continue
            found_artifact = False
            for value in artifact_paths:
                if isinstance(value, str):
                    candidate = Path(value)
                    if candidate.is_file():
                        paths.add(candidate.resolve())
                        found_artifact = True
            if not found_artifact and derived_path is not None and derived_path.is_file():
                paths.add(derived_path.resolve())
        # Always scan legacy/private artifacts because a partially migrated history can
        # contain one modern record alongside many rows without artifact metadata. The
        # caller intersects extras with existing Dense/BM25 IDs and the current source,
        # preventing unindexed disk orphans from being resurrected.
        paths.update(path.resolve() for path in self.chunks_dir.glob("*.jsonl"))
        paths.difference_update(superseded_paths)
        _, current_chunks = self.artifact_paths(entry.document_id, collection=collection)
        if not current_chunks.is_file():
            raise FileNotFoundError(f"Current chunk artifact is missing: {current_chunks}")
        paths.add(current_chunks.resolve())
        return tuple(sorted(paths, key=lambda path: path.as_posix()))

    def _derived_chunk_artifact(self, record: IngestionRecord) -> Path | None:
        document_id = record.metadata.get("document_id")
        if not isinstance(document_id, str):
            document_id = BaseLoader.build_document_id(record.file_hash)
            record_collection = record.collection or "wms-system-training"
            document_id = self._storage_document_id(document_id, record_collection)
        return self.chunks_dir / f"{document_id}.jsonl"

    def finalize_indexing(self, entry: CorpusManifestEntry, *, collection: str) -> int:
        """Commit the current hash inside the coordinated index transaction."""

        collection = self._validate_collection(collection)
        document_output, chunks_output = self.artifact_paths(
            entry.document_id,
            collection=collection,
        )
        chunks = self._read_chunks(chunks_output)
        document_id = document_output.stem
        fallbacks = collect_llm_fallbacks(
            chunks,
            document_id=document_id,
            source_path=entry.source_path,
        )
        self._mark_success(
            entry,
            collection=collection,
            document_id=document_id,
            document_output=document_output,
            chunks_output=chunks_output,
            chunk_count=len(chunks),
            llm_fallback_count=len(fallbacks),
            processing_signature=self._collection_signature(collection),
        )

        return len(self._superseded_records(entry, collection=collection))

    def finalize_optional_assets(self, entry: CorpusManifestEntry, *, collection: str) -> None:
        """Register optional image assets after the core stores and history commit."""

        document_output, _ = self.artifact_paths(entry.document_id, collection=collection)
        payload = json.loads(document_output.read_text(encoding="utf-8"))
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return
        file_hash = metadata.get("file_hash")
        if self.image_storage is not None and isinstance(file_hash, str):
            storage_collection = (
                self.image_collection if collection == "wms-system-training" else collection
            )
            self.image_storage.remove_document(
                file_hash,
                collection=storage_collection,
            )
        self._store_document_images(metadata, collection=collection)
        self._write_json(document_output, payload)

    def cleanup_superseded(
        self,
        entry: CorpusManifestEntry,
        *,
        collection: str,
    ) -> dict[str, object]:
        """Best-effort cleanup that never reverses an already consistent commit."""

        collection = self._validate_collection(collection)
        superseded = self._superseded_records(entry, collection=collection)
        ledger = LLMFailureLedger(self.output_root / "llm_failures.jsonl")
        removed = 0
        errors: list[str] = []
        for record in superseded:
            try:
                if self.image_storage is not None:
                    storage_collection = (
                        self.image_collection if collection == "wms-system-training" else collection
                    )
                    self.image_storage.remove_document(
                        record.file_hash,
                        collection=storage_collection,
                    )
                self._delete_superseded_artifacts(record)
                old_document_id = record.metadata.get("document_id")
                if isinstance(old_document_id, str):
                    ledger.update_document(old_document_id, ())
                self.integrity.remove_record(
                    file_hash=record.file_hash,
                    collection=collection,
                )
            except (OSError, ValueError, sqlite3.Error) as exc:
                errors.append(type(exc).__name__)
            else:
                removed += 1
        if superseded:
            try:
                ledger.write()
            except OSError as exc:
                errors.append(type(exc).__name__)
        return {
            "target_count": len(superseded),
            "removed_count": removed,
            "status": "ok" if not errors else "partial",
            "error_types": tuple(errors),
        }

    def mark_indexing_failed(
        self,
        entry: CorpusManifestEntry,
        *,
        collection: str,
        error: BaseException,
    ) -> None:
        """Ensure an indexing failure can never satisfy the incremental skip check."""

        self._mark_failed(
            entry,
            self._validate_collection(collection),
            f"indexing:{type(error).__name__}",
        )

    def _superseded_records(
        self,
        entry: CorpusManifestEntry,
        *,
        collection: str,
    ) -> list[IngestionRecord]:
        source_key = self._path_key(entry.source_path)
        return [
            record
            for record in self.integrity.list_processed(
                status="success",
                collection=collection,
            )
            if record.file_hash != entry.file_hash
            and self._path_key(
                str(
                    record.metadata.get("source_relative_path")
                    or record.metadata.get("source_path")
                    or record.file_path
                )
            )
            == source_key
        ]

    def _delete_superseded_artifacts(self, record: IngestionRecord) -> None:
        candidates: list[tuple[Path, Path]] = []
        document_path = record.metadata.get("document_artifact_path")
        if isinstance(document_path, str):
            candidates.append((Path(document_path), self.documents_dir))
        chunk_paths = record.metadata.get("chunk_artifact_paths")
        if isinstance(chunk_paths, list | tuple):
            candidates.extend(
                (Path(value), self.chunks_dir) for value in chunk_paths if isinstance(value, str)
            )
        image_paths = record.metadata.get("extracted_image_artifact_paths")
        if isinstance(image_paths, list | tuple):
            candidates.extend(
                (Path(value), self.images_dir) for value in image_paths if isinstance(value, str)
            )
        for candidate, root in candidates:
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"Refusing to remove artifact outside managed root: {resolved}"
                ) from exc
            if root == self.images_dir and self._image_referenced_by_other_record(
                resolved,
                record,
            ):
                continue
            resolved.unlink(missing_ok=True)

    def _image_referenced_by_other_record(
        self,
        image_path: Path,
        owner: IngestionRecord,
    ) -> bool:
        for record in self.integrity.list_processed(status="success"):
            if record.file_hash == owner.file_hash and record.collection == owner.collection:
                continue
            values = record.metadata.get("extracted_image_artifact_paths")
            if isinstance(values, list | tuple) and any(
                isinstance(value, str) and Path(value).resolve() == image_path for value in values
            ):
                return True
        return False

    @staticmethod
    def _path_key(value: str) -> str:
        return value.replace("\\", "/").removeprefix("./").casefold()

    @staticmethod
    def _validate_collection(collection: str) -> str:
        if not isinstance(collection, str) or not collection.strip():
            raise ValueError("collection must be a non-empty string")
        return collection.strip()

    @staticmethod
    def _storage_document_id(document_id: str, collection: str) -> str:
        """Namespace new collections while preserving the existing private-corpus IDs."""
        if collection == "wms-system-training":
            return document_id
        digest = hashlib.sha256(f"{collection}\0{document_id}".encode()).hexdigest()
        return f"doc-{digest[:16]}"

    def _collection_signature(self, collection: str) -> str:
        if collection == "wms-system-training":
            return self.processing_signature
        payload = f"{self.processing_signature}\0{collection}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def _should_skip(self, file_hash: str, collection: str, signature: str) -> bool:
        parameters = inspect.signature(self.integrity.should_skip).parameters
        if "collection" in parameters:
            return self.integrity.should_skip(
                file_hash,
                processing_signature=signature,
                collection=collection,
            )
        return self.integrity.should_skip(file_hash, processing_signature=signature)

    def _mark_success(
        self,
        entry: CorpusManifestEntry,
        *,
        collection: str,
        document_id: str,
        document_output: Path,
        chunks_output: Path,
        chunk_count: int,
        llm_fallback_count: int,
        processing_signature: str,
    ) -> None:
        source_path = (self.source_root / entry.source_path).resolve()
        extracted_image_paths = [str(path) for path in self._document_image_paths(document_output)]
        if not extracted_image_paths and self.extract_images:
            existing = self.capture_history(entry, collection=collection)
            existing_paths = (
                existing.metadata.get("extracted_image_artifact_paths")
                if existing is not None
                else None
            )
            if isinstance(existing_paths, list | tuple):
                extracted_image_paths = [
                    str(value) for value in existing_paths if isinstance(value, str)
                ]
        self.integrity.mark_success(
            entry.file_hash,
            entry.source_path,
            collection=collection,
            source_path=entry.source_path,
            source_relative_path=entry.source_path,
            staged_pdf_path=str(source_path),
            document_id=document_id,
            document_artifact_path=str(document_output.resolve()),
            chunk_artifact_paths=[str(chunks_output.resolve())],
            extracted_image_artifact_paths=extracted_image_paths,
            chunk_count=chunk_count,
            llm_fallback_count=llm_fallback_count,
            processing_signature=processing_signature,
        )

    def _mark_failed(self, entry: CorpusManifestEntry, collection: str, error_message: str) -> None:
        parameters = inspect.signature(self.integrity.mark_failed).parameters
        if "collection" in parameters:
            self.integrity.mark_failed(
                entry.file_hash,
                error_message,
                entry.source_path,
                collection=collection,
            )
            return
        self.integrity.mark_failed(entry.file_hash, error_message, entry.source_path)

    @staticmethod
    def _emit_progress(
        callback: CorpusProgressCallback | None,
        stage: str,
        current: int,
        total: int,
    ) -> None:
        if callback is not None:
            callback(stage, current, total)

    @staticmethod
    def _record_stage(
        trace: object | None,
        name: str,
        started: float,
        details: dict[str, object],
    ) -> None:
        if trace is not None and hasattr(trace, "record_stage"):
            trace.record_stage(
                name,
                (time.perf_counter() - started) * 1000,
                details=details,
            )

    def _retry_failed_chunks(self, chunks: list[Chunk]) -> tuple[list[Chunk], int]:
        """Rerun each failed chunk from its earliest active failing transform onward."""

        result: list[Chunk] = []
        retried = 0
        for chunk in chunks:
            first_failed = next(
                (
                    index
                    for index, transform in enumerate(self.transforms)
                    if self._has_active_failure(chunk, transform)
                ),
                None,
            )
            if first_failed is None:
                result.append(chunk)
                continue
            transformed = [chunk]
            for transform in self.transforms[first_failed:]:
                transformed = transform.transform(transformed)
            result.extend(transformed)
            retried += 1
        return result, retried

    @staticmethod
    def _has_active_failure(chunk: Chunk, transform: BaseTransform) -> bool:
        metadata = chunk.metadata
        name = getattr(transform, "name", "")
        if name == "chunk_refiner":
            reason = metadata.get("refinement_fallback_reason")
            return bool(
                getattr(transform, "enabled", False)
                and getattr(transform, "use_llm", False)
                and isinstance(reason, str)
                and reason != "empty_rule_result"
            )
        if name == "metadata_enricher":
            return bool(
                getattr(transform, "enabled", False)
                and getattr(transform, "use_llm", False)
                and isinstance(metadata.get("metadata_enrichment_fallback_reason"), str)
            )
        if name == "image_captioner":
            return bool(
                getattr(transform, "enabled", False)
                and metadata.get("image_caption_status")
                in {"failed", "partial", "vision_llm_unavailable"}
            )
        return False

    @staticmethod
    def _read_chunks(path: Path) -> list[Chunk]:
        chunks: list[Chunk] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                chunks.append(Chunk(**json.loads(line)))
        return chunks

    def _store_document_images(self, metadata: dict[str, object], *, collection: str) -> None:
        images = metadata.get("images")
        if self.image_storage is None or not isinstance(images, list) or not images:
            return
        extracted_paths = [
            str(Path(image["path"]).resolve())
            for image in images
            if isinstance(image, dict)
            and isinstance(image.get("path"), str)
            and self._is_managed_extracted_image(Path(image["path"]))
        ]
        if extracted_paths:
            metadata["extracted_image_artifact_paths"] = extracted_paths
        storage_collection = (
            self.image_collection if collection == "wms-system-training" else collection
        )
        try:
            metadata["images"] = self.image_storage.store_metadata_images(
                images,
                collection=storage_collection,
                doc_hash=(
                    str(metadata["file_hash"])
                    if isinstance(metadata.get("file_hash"), str)
                    else None
                ),
            )
        except (OSError, ValueError, sqlite3.Error):
            # Extracted paths remain valid even if the optional image index is unavailable.
            metadata["image_storage_status"] = "fallback_to_extracted_paths"

    def _processing_signature(self) -> str:
        splitter = self.chunker.splitter
        payload: dict[str, object] = {
            "schema_version": 2,
            "extract_images": self.extract_images,
            "image_collection": self.image_collection,
            "splitter": {
                "class": self._class_name(splitter),
                "chunk_size": getattr(splitter, "chunk_size", None),
                "chunk_overlap": getattr(splitter, "chunk_overlap", None),
                "implementation": self._implementation_hash(splitter),
            },
            "transforms": [self._transform_signature(item) for item in self.transforms],
            "image_storage": (
                {
                    "class": self._class_name(self.image_storage),
                    "root_path": str(self.image_storage.root_path),
                }
                if self.image_storage is not None
                else None
            ),
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def _transform_signature(cls, transform: BaseTransform) -> dict[str, object]:
        llm = getattr(transform, "llm", None)
        vision_llm = getattr(transform, "vision_llm", None)
        llm_active = bool(getattr(transform, "use_llm", False))
        vision_llm_active = bool(getattr(transform, "enabled", False) and vision_llm is not None)
        prompt = getattr(transform, "prompt", None)
        return {
            "class": cls._class_name(transform),
            "implementation": cls._implementation_hash(transform),
            "enabled": getattr(transform, "enabled", None),
            "use_llm": getattr(transform, "use_llm", None),
            "append_to_text": getattr(transform, "append_to_text", None),
            "prompt_hash": (
                hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                if isinstance(prompt, str) and (llm_active or vision_llm_active)
                else None
            ),
            "llm_class": cls._class_name(llm) if llm_active and llm is not None else None,
            "llm_model": getattr(llm, "model", None) if llm_active and llm is not None else None,
            "vision_llm_class": (
                cls._class_name(vision_llm)
                if vision_llm_active and vision_llm is not None
                else None
            ),
            "vision_llm_model": (
                getattr(vision_llm, "model", None)
                if vision_llm_active and vision_llm is not None
                else None
            ),
        }

    @staticmethod
    def _class_name(value: object) -> str:
        value_type = type(value)
        return f"{value_type.__module__}.{value_type.__qualname__}"

    @staticmethod
    def _implementation_hash(value: object) -> str | None:
        try:
            source = inspect.getsource(type(value))
        except (OSError, TypeError):
            return None
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    @staticmethod
    def _domain_metadata(
        entry: CorpusManifestEntry, collection: str = "wms-system-training"
    ) -> dict[str, object]:
        return {
            "title": entry.title,
            "collection": collection,
            "version": entry.version,
            "module": entry.domain,
            "domain": entry.domain,
            "process_stage": entry.process_stage,
            "process_code": entry.process_code,
            "document_type": entry.document_type,
            "source_relative_path": entry.source_path,
            "related_document_paths": list(entry.related_document_paths),
        }

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        CorpusProcessor._replace_with_retry(temporary, path)

    @staticmethod
    def _write_jsonl(path: Path, payloads: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        content = "".join(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n" for payload in payloads
        )
        temporary.write_text(content, encoding="utf-8")
        CorpusProcessor._replace_with_retry(temporary, path)

    @staticmethod
    def _replace_with_retry(temporary: Path, destination: Path) -> None:
        """Handle transient Windows readers that briefly lock an existing artifact."""
        for attempt in range(6):
            try:
                temporary.replace(destination)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (2**attempt))
