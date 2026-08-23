import json
from pathlib import Path

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
        splitter_settings=SplitterSettings(
            provider="recursive", chunk_size=100, chunk_overlap=10
        ),
        loader_builder=loader_builder,
    )

    first = processor.process([entry])
    second = processor.process([entry])

    assert first.succeeded == 1
    assert first.chunks_written > 1
    assert second.skipped == 1
    document_path = tmp_path / "processed" / "documents" / f"{entry.document_id}.json"
    chunks_path = tmp_path / "processed" / "chunks" / f"{entry.document_id}.jsonl"
    assert json.loads(document_path.read_text(encoding="utf-8"))["metadata"][
        "process_code"
    ] == "SWL.I.01.01"
    assert len(chunks_path.read_text(encoding="utf-8").splitlines()) == first.chunks_written


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
        splitter_settings=SplitterSettings(
            provider="recursive", chunk_size=100, chunk_overlap=10
        ),
        loader_builder=failing_loader,
    )

    report = processor.process([entry])

    assert report.failed == 1
    assert report.errors[0]["error_type"] == "RuntimeError"
    assert (tmp_path / "processed" / "processing_report.json").is_file()
