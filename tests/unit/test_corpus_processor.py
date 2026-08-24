import json
import sqlite3
from pathlib import Path

import pytest

from core.settings import SplitterSettings
from core.types import Document
from ingestion.corpus_manifest import CorpusManifestBuilder
from ingestion.corpus_processor import CorpusProcessor
from libs.loader.base_loader import BaseLoader


class FakeLoader(BaseLoader):
    def load(self, path, metadata=None) -> Document:
        file_path = Path(path)
        file_hash = self.compute_file_hash(file_path)
        values = dict(metadata or {})
        values["source_path"] = file_path.as_posix()
        return Document(
            id=self.build_document_id(file_hash),
            text="MOCA configuration location and setup instructions. " * 8,
            metadata=values,
        )


class ImageFakeLoader(BaseLoader):
    def load(self, path, metadata=None) -> Document:
        file_path = Path(path)
        image_path = file_path.with_suffix(".png")
        image_path.write_bytes(b"image")
        values = dict(metadata or {})
        values.update(
            {
                "source_path": file_path.as_posix(),
                "file_hash": self.compute_file_hash(file_path),
                "images": [{"id": "image-1", "path": str(image_path), "page": 1}],
                "pages": [{"page": 1, "start_offset": 0, "end_offset": 30}],
            }
        )
        return Document(
            id="image-doc",
            text="Diagram\n[IMAGE: image-1]",
            metadata=values,
        )


def _entry(source: Path):
    file_hash = BaseLoader.compute_file_hash(source)
    return CorpusManifestBuilder().read(
        _write_manifest(
            source.parent / "manifest.jsonl",
            file_hash=file_hash,
            source_name=source.name,
        )
    )[0]


def _write_manifest(path: Path, *, file_hash: str, source_name: str) -> Path:
    payload = {
        "schema_version": 1,
        "document_id": BaseLoader.build_document_id(file_hash),
        "file_hash": file_hash,
        "source_path": source_name,
        "source_name": source_name,
        "title": "Test configuration",
        "process_code": "SWL.I.01.01",
        "domain": "Inbound",
        "process_stage": "I1.Pre-Receiving",
        "document_type": "configuration",
        "page_count": 1,
        "size_bytes": 8,
        "modified_at": "2026-08-23T00:00:00+00:00",
        "version": "test",
        "related_document_paths": [],
        "duplicate_of": None,
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def test_processor_writes_private_artifacts_and_skips_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "SWL.I.01.01 Test.pdf"
    source.write_bytes(b"fake-pdf")
    entry = _entry(source)

    def loader_builder(*args, **kwargs):
        return FakeLoader()

    processor = CorpusProcessor(
        source_root=tmp_path,
        output_root=tmp_path / "processed",
        database_path=tmp_path / "db" / "history.db",
        splitter_settings=SplitterSettings(provider="recursive", chunk_size=100, chunk_overlap=10),
        loader_builder=loader_builder,
    )

    first = processor.process([entry])
    second = processor.process([entry])
    changed_processor = CorpusProcessor(
        source_root=tmp_path,
        output_root=tmp_path / "processed",
        database_path=tmp_path / "db" / "history.db",
        splitter_settings=SplitterSettings(provider="recursive", chunk_size=120, chunk_overlap=10),
        loader_builder=loader_builder,
    )
    after_setting_change = changed_processor.process([entry])

    assert first.succeeded == 1
    assert first.chunks_written > 1
    assert second.skipped == 1
    assert after_setting_change.succeeded == 1
    document_path = tmp_path / "processed" / "documents" / f"{entry.document_id}.json"
    chunks_path = tmp_path / "processed" / "chunks" / f"{entry.document_id}.jsonl"
    assert (
        json.loads(document_path.read_text(encoding="utf-8"))["metadata"]["process_code"]
        == "SWL.I.01.01"
    )
    assert (
        len(chunks_path.read_text(encoding="utf-8").splitlines())
        == after_setting_change.chunks_written
    )


def test_processor_records_failure_without_stopping_batch(tmp_path: Path) -> None:
    source = tmp_path / "SWL.I.01.01 Test.pdf"
    source.write_bytes(b"fake-pdf")
    entry = _entry(source)

    def failing_loader(*args, **kwargs):
        raise RuntimeError("parser unavailable")

    processor = CorpusProcessor(
        source_root=tmp_path,
        output_root=tmp_path / "processed",
        database_path=tmp_path / "db" / "history.db",
        splitter_settings=SplitterSettings(provider="recursive", chunk_size=100, chunk_overlap=10),
        loader_builder=failing_loader,
    )

    report = processor.process([entry])

    assert report.failed == 1
    assert report.errors[0]["error_type"] == "RuntimeError"
    assert (tmp_path / "processed" / "processing_report.json").is_file()


def test_atomic_replace_retries_transient_windows_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TemporarilyLockedPath:
        calls = 0

        def replace(self, destination) -> None:
            self.calls += 1
            if self.calls < 3:
                raise PermissionError("temporarily locked")

    temporary = TemporarilyLockedPath()
    monkeypatch.setattr("ingestion.corpus_processor.time.sleep", lambda _: None)

    CorpusProcessor._replace_with_retry(temporary, Path("destination"))

    assert temporary.calls == 3


def test_optional_image_index_sqlite_failure_does_not_fail_document(tmp_path: Path) -> None:
    source = tmp_path / "SWL.I.99.01 Image Test.pdf"
    source.write_bytes(b"fake-pdf")
    entry = _entry(source)

    class BrokenImageStorage:
        root_path = tmp_path / "images"

        @staticmethod
        def store_metadata_images(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    processor = CorpusProcessor(
        source_root=tmp_path,
        output_root=tmp_path / "processed",
        database_path=tmp_path / "history.db",
        splitter_settings=SplitterSettings(provider="recursive", chunk_size=100, chunk_overlap=10),
        image_storage=BrokenImageStorage(),
        loader_builder=lambda *args, **kwargs: ImageFakeLoader(),
    )

    report = processor.process([entry])
    document = json.loads(
        (tmp_path / "processed" / "documents" / f"{entry.document_id}.json").read_text(
            encoding="utf-8"
        )
    )

    assert report.succeeded == 1
    assert report.failed == 0
    assert document["metadata"]["image_storage_status"] == "fallback_to_extracted_paths"


def test_inactive_generation_provider_does_not_change_processing_signature() -> None:
    class FirstProvider:
        model = "first"

    class SecondProvider:
        model = "second"

    class Transform:
        enabled = True
        use_llm = False
        append_to_text = None
        prompt = "prompt"
        vision_llm = None

        def __init__(self, llm) -> None:
            self.llm = llm

    first = Transform(FirstProvider())
    second = Transform(SecondProvider())

    assert CorpusProcessor._transform_signature(first) == CorpusProcessor._transform_signature(
        second
    )

    first.use_llm = True
    second.use_llm = True
    assert CorpusProcessor._transform_signature(first) != CorpusProcessor._transform_signature(
        second
    )
