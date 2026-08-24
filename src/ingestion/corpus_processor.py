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
from core.types import Chunk
from ingestion.chunking import DocumentChunker
from ingestion.corpus_manifest import CorpusManifestEntry
from ingestion.llm_failure_ledger import LLMFailureLedger, collect_llm_fallbacks
from ingestion.storage import ImageStorage
from ingestion.transform import BaseTransform
from libs.loader import BaseLoader, LoaderFactory, SQLiteIntegrityChecker

LoaderBuilder = Callable[..., BaseLoader]


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
    ) -> CorpusProcessingReport:
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
            document_output = self.documents_dir / f"{entry.document_id}.json"
            chunks_output = self.chunks_dir / f"{entry.document_id}.jsonl"
            can_skip = (
                not force
                and self.integrity.should_skip(
                    entry.file_hash,
                    processing_signature=self.processing_signature,
                )
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
                            document_id=entry.document_id,
                            source_path=entry.source_path,
                        )
                        self._write_jsonl(chunks_output, [chunk.to_dict() for chunk in chunks])
                        failure_ledger.update_document(entry.document_id, fallbacks)
                        self.integrity.mark_success(
                            entry.file_hash,
                            entry.source_path,
                            document_id=entry.document_id,
                            chunk_count=len(chunks),
                            llm_fallback_count=len(fallbacks),
                            processing_signature=self.processing_signature,
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
                skipped += 1
                continue

            source_path = self.source_root / Path(entry.source_path)
            try:
                loader = self.loader_builder(
                    source_path,
                    image_output_dir=self.images_dir,
                    extract_images=self.extract_images,
                )
                document = loader.load(source_path, self._domain_metadata(entry))
                self._store_document_images(document.metadata)
                chunks = self.chunker.split_document(document)
                for transform in self.transforms:
                    chunks = transform.transform(chunks)
                fallbacks = collect_llm_fallbacks(
                    chunks,
                    document_id=entry.document_id,
                    source_path=entry.source_path,
                )
                self._write_json(document_output, document.to_dict())
                self._write_jsonl(chunks_output, [chunk.to_dict() for chunk in chunks])
                failure_ledger.update_document(entry.document_id, fallbacks)
                self.integrity.mark_success(
                    entry.file_hash,
                    entry.source_path,
                    document_id=entry.document_id,
                    chunk_count=len(chunks),
                    llm_fallback_count=len(fallbacks),
                    processing_signature=self.processing_signature,
                )
                succeeded += 1
                chunks_written += len(chunks)
            except Exception as exc:
                failed += 1
                error = {
                    "source_path": entry.source_path,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
                errors.append(error)
                self.integrity.mark_failed(
                    entry.file_hash,
                    str(exc),
                    entry.source_path,
                )
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

    def _store_document_images(self, metadata: dict[str, object]) -> None:
        images = metadata.get("images")
        if self.image_storage is None or not isinstance(images, list) or not images:
            return
        try:
            metadata["images"] = self.image_storage.store_metadata_images(
                images,
                collection=self.image_collection,
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
    def _domain_metadata(entry: CorpusManifestEntry) -> dict[str, object]:
        return {
            "title": entry.title,
            "collection": "wms-system-training",
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
