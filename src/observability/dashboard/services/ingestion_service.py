"""Safe Dashboard orchestration for staged PDF ingestion and confirmed deletion."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ingestion import (
    DeleteResult,
    DocumentInfo,
    DocumentManager,
    IngestionPipeline,
    IngestionResult,
)
from libs.atomic_file import replace_file_atomically

DashboardProgressCallback = Callable[[str, int, int], None]

_COLLECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_PDF_HEADER_BYTES = 1024
_PROGRESS_STAGES = ("load", "split", "transform", "embed", "upsert")


@dataclass(frozen=True, slots=True)
class BoundedProgress:
    stage: str
    current: int
    total: int
    fraction: float


class IngestionService:
    """Validate uploads before allowing the reusable ingestion pipeline to mutate stores."""

    def __init__(
        self,
        pipeline: IngestionPipeline,
        document_manager: DocumentManager,
        *,
        staging_root: str | Path,
        max_upload_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be greater than 0")
        self.pipeline = pipeline
        self.document_manager = document_manager
        self.staging_root = Path(staging_root).resolve()
        self.max_upload_bytes = max_upload_bytes

    def ingest_pdf(
        self,
        filename: str,
        payload: bytes,
        collection: str,
        *,
        force: bool = False,
        on_progress: DashboardProgressCallback | None = None,
    ) -> IngestionResult:
        collection = self.validate_collection(collection)
        self.validate_upload(filename, payload)
        staged_path, created = self._stage_upload(filename, payload)
        try:
            return self.pipeline.run(
                staged_path,
                collection=collection,
                force=force,
                on_progress=on_progress,
            )
        except Exception:
            if created:
                staged_path.unlink(missing_ok=True)
            raise

    def list_documents(self) -> list[DocumentInfo]:
        return self.document_manager.list_documents()

    @staticmethod
    def deletion_phrase(document: DocumentInfo) -> str:
        return f"DELETE {document.doc_id[:12]}"

    def delete_document(
        self,
        doc_id: str,
        *,
        confirmation: str,
    ) -> DeleteResult:
        document = next(
            (item for item in self.list_documents() if item.doc_id == doc_id),
            None,
        )
        if document is None:
            raise KeyError(f"Unknown document: {doc_id}")
        expected = self.deletion_phrase(document)
        if confirmation.strip() != expected:
            raise ValueError(f"Deletion confirmation must exactly match: {expected}")
        return self.document_manager.delete_document(
            document.source_path,
            document.collection,
        )

    def validate_upload(self, filename: str, payload: bytes) -> None:
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("Upload filename must not be empty")
        normalized = filename.replace("\\", "/")
        if Path(normalized).name != normalized or normalized in {".", ".."}:
            raise ValueError("Upload filename must not contain a directory path")
        if len(normalized) > 180:
            raise ValueError("Upload filename must not exceed 180 characters")
        if Path(normalized).suffix.casefold() != ".pdf":
            raise ValueError("Only PDF uploads are accepted")
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("Uploaded PDF must not be empty")
        if len(payload) > self.max_upload_bytes:
            raise ValueError(
                f"Uploaded PDF exceeds the {self.max_upload_bytes // (1024 * 1024)} MiB limit"
            )
        if b"%PDF-" not in payload[:_PDF_HEADER_BYTES]:
            raise ValueError("Uploaded content does not have a valid PDF header")

    @staticmethod
    def validate_collection(collection: str) -> str:
        if not isinstance(collection, str):
            raise ValueError("Collection must be a string")
        normalized = collection.strip()
        if not _COLLECTION.fullmatch(normalized):
            raise ValueError(
                "Collection must be 1-64 characters using letters, numbers, "
                "dot, dash, or underscore"
            )
        return normalized

    @staticmethod
    def bounded_progress(stage: str, current: int, total: int) -> BoundedProgress:
        stage_index = _PROGRESS_STAGES.index(stage) if stage in _PROGRESS_STAGES else 0
        safe_total = max(int(total), 1)
        safe_current = min(max(int(current), 0), safe_total)
        fraction = (stage_index + safe_current / safe_total) / len(_PROGRESS_STAGES)
        return BoundedProgress(stage, safe_current, safe_total, min(max(fraction, 0.0), 1.0))

    def _stage_upload(self, filename: str, payload: bytes) -> tuple[Path, bool]:
        self.staging_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(payload).hexdigest()
        safe_stem = _SAFE_FILENAME.sub("-", Path(filename).stem).strip(".-")[:100] or "upload"
        destination = (self.staging_root / f"{digest[:24]}-{safe_stem}.pdf").resolve()
        destination.relative_to(self.staging_root)
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
            return destination, False
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.upload.tmp")
        try:
            temporary.write_bytes(payload)
            replace_file_atomically(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination, True
