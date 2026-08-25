from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ingestion import DeleteResult, DocumentInfo
from observability.dashboard.services import IngestionService


class _Pipeline:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[Path, str, bool]] = []

    def run(self, path, *, collection, force, on_progress):
        source = Path(path)
        self.calls.append((source, collection, force))
        if on_progress:
            on_progress("load", 1, 1)
            on_progress("upsert", 2, 2)
        if self.failure:
            raise self.failure
        return SimpleNamespace(source_path=source.name, collection=collection)


class _Manager:
    def __init__(self) -> None:
        self.document = DocumentInfo(
            "0123456789abcdef",
            "staging/manual.pdf",
            "manuals",
            2,
            0,
            None,
            "file-hash",
            "Manual",
        )
        self.deleted: list[tuple[str, str]] = []

    def list_documents(self):
        return [self.document]

    def delete_document(self, source_path, collection):
        self.deleted.append((source_path, collection))
        return DeleteResult(source_path, collection, 2, 2, 0, 1, 2)


def _service(tmp_path: Path, *, pipeline: _Pipeline | None = None) -> IngestionService:
    return IngestionService(
        pipeline or _Pipeline(),  # type: ignore[arg-type]
        _Manager(),  # type: ignore[arg-type]
        staging_root=tmp_path / "staging",
    )


def test_ingestion_service_stages_pdf_and_forwards_bounded_progress(tmp_path: Path) -> None:
    pipeline = _Pipeline()
    service = _service(tmp_path, pipeline=pipeline)
    progress: list[tuple[str, int, int]] = []

    result = service.ingest_pdf(
        "SWL.I.01.01 Manual.pdf",
        b"%PDF-1.4\nfixture",
        "manuals-v1",
        force=True,
        on_progress=lambda *event: progress.append(event),
    )

    staged_path, collection, force = pipeline.calls[0]
    assert staged_path.parent == (tmp_path / "staging").resolve()
    assert staged_path.read_bytes() == b"%PDF-1.4\nfixture"
    assert collection == result.collection == "manuals-v1"
    assert force is True
    assert progress == [("load", 1, 1), ("upsert", 2, 2)]


@pytest.mark.parametrize(
    ("filename", "payload", "collection", "message"),
    [
        ("../manual.pdf", b"%PDF-1.4", "manuals", "directory path"),
        ("manual.txt", b"%PDF-1.4", "manuals", "Only PDF"),
        ("manual.pdf", b"not-a-pdf", "manuals", "valid PDF header"),
        ("manual.pdf", b"%PDF-1.4", "bad collection", "Collection must be"),
    ],
)
def test_ingestion_service_rejects_invalid_uploads(
    tmp_path: Path,
    filename: str,
    payload: bytes,
    collection: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _service(tmp_path).ingest_pdf(filename, payload, collection)


def test_ingestion_service_removes_new_stage_file_after_pipeline_failure(tmp_path: Path) -> None:
    service = _service(tmp_path, pipeline=_Pipeline(failure=RuntimeError("provider down")))

    with pytest.raises(RuntimeError, match="provider down"):
        service.ingest_pdf("manual.pdf", b"%PDF-1.4\nfixture", "manuals")

    assert list((tmp_path / "staging").glob("*.pdf")) == []


def test_ingestion_service_rejects_upload_over_configured_size(tmp_path: Path) -> None:
    service = IngestionService(
        _Pipeline(),  # type: ignore[arg-type]
        _Manager(),  # type: ignore[arg-type]
        staging_root=tmp_path,
        max_upload_bytes=8,
    )

    with pytest.raises(ValueError, match="exceeds"):
        service.ingest_pdf("manual.pdf", b"%PDF-1.4\n", "manuals")


def test_ingestion_service_requires_exact_confirmation_before_deletion(tmp_path: Path) -> None:
    manager = _Manager()
    service = IngestionService(
        _Pipeline(),  # type: ignore[arg-type]
        manager,  # type: ignore[arg-type]
        staging_root=tmp_path,
    )

    with pytest.raises(ValueError, match="exactly match"):
        service.delete_document(manager.document.doc_id, confirmation="DELETE")
    result = service.delete_document(
        manager.document.doc_id,
        confirmation="DELETE 0123456789ab",
    )

    assert result.success is True
    assert manager.deleted == [("staging/manual.pdf", "manuals")]


def test_dashboard_progress_is_clamped_to_zero_and_one() -> None:
    assert IngestionService.bounded_progress("load", -5, 0).fraction == 0.0
    assert IngestionService.bounded_progress("upsert", 999, 2).fraction == 1.0
