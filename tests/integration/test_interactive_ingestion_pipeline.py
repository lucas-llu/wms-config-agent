from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pypdf import PdfWriter

from core.settings import SplitterSettings
from core.trace import TraceCollector
from core.types import Chunk, Document
from ingestion import CorpusProcessor, IndexingPipeline, IngestionPipeline
from ingestion.storage import BM25Indexer, ImageStorage
from ingestion.transform import BaseTransform
from libs.embedding import LocalLSAEmbedding
from libs.loader import BaseLoader, SQLiteIntegrityChecker
from libs.vector_store import ChromaStore

pytestmark = pytest.mark.integration


class FixtureLoader(BaseLoader):
    def load(self, path, metadata=None) -> Document:
        source = Path(path)
        values = dict(metadata or {})
        values.update(
            {
                "source_path": source.as_posix(),
                "file_hash": self.compute_file_hash(source),
                "images": [],
            }
        )
        text = "\n\n".join(
            [
                "Directed putaway configuration selects storage locations and zones.",
                "Inbound appointment configuration controls dock capacity and schedules.",
                "MOCA policy settings define warehouse execution behavior.",
                "Outbound staging configuration assigns lanes and shipping doors.",
            ]
        )
        return Document(
            id=self.build_document_id(values["file_hash"]),
            text=text,
            metadata=values,
        )


class IdentityTransform(BaseTransform):
    name = "identity"

    def transform(self, chunks, trace=None):
        return [self.clone_chunk(chunk) for chunk in chunks]


class ImageFixtureLoader(FixtureLoader):
    def __init__(self, image_output_dir: Path) -> None:
        self.image_output_dir = Path(image_output_dir)

    def load(self, path, metadata=None) -> Document:
        document = super().load(path, metadata)
        file_hash = self.compute_file_hash(path)
        image_path = self.image_output_dir / f"{file_hash}-page-1.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(f"image:{file_hash}".encode())
        values = dict(document.metadata)
        values["images"] = [
            {
                "id": f"image-{file_hash[:12]}",
                "path": str(image_path.resolve()),
                "page": 1,
            }
        ]
        return Document(id=document.id, text=document.text, metadata=values)


def _write_pdf(path: Path, marker: str = "v1") -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Fixture-Version": marker})
    with path.open("wb") as stream:
        writer.write(stream)


def _fixture_pipeline(
    tmp_path: Path,
    staging: Path,
    *,
    trace_name: str = "traces.jsonl",
) -> tuple[IngestionPipeline, Path]:
    history_path = tmp_path / "db" / "ingestion_history.db"
    processor = CorpusProcessor(
        source_root=staging,
        output_root=tmp_path / "processed",
        database_path=history_path,
        splitter_settings=SplitterSettings(
            provider="recursive",
            chunk_size=80,
            chunk_overlap=10,
        ),
        transforms=(IdentityTransform(),),
        loader_builder=lambda *args, **kwargs: FixtureLoader(),
    )
    indexing = IndexingPipeline(
        embedding=LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models"),
        vector_store=ChromaStore(
            persist_path=tmp_path / "chroma",
            collection_name="chunks",
        ),
        bm25_indexer=BM25Indexer(tmp_path / "bm25"),
        batch_size=2,
    )
    return (
        IngestionPipeline(
            corpus_processor=processor,
            indexing_pipeline=indexing,
            trace_collector=TraceCollector(tmp_path / "logs" / trace_name),
        ),
        history_path,
    )


def _image_fixture_pipeline(
    tmp_path: Path,
    staging: Path,
) -> tuple[IngestionPipeline, ImageStorage, Path]:
    history_path = tmp_path / "db" / "ingestion_history.db"
    image_storage = ImageStorage(
        tmp_path / "managed-images",
        tmp_path / "db" / "images.db",
    )
    processor = CorpusProcessor(
        source_root=staging,
        output_root=tmp_path / "processed",
        database_path=history_path,
        splitter_settings=SplitterSettings(
            provider="recursive",
            chunk_size=80,
            chunk_overlap=10,
        ),
        extract_images=True,
        transforms=(IdentityTransform(),),
        image_storage=image_storage,
        loader_builder=lambda *args, **kwargs: ImageFixtureLoader(kwargs["image_output_dir"]),
    )
    indexing = IndexingPipeline(
        embedding=LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models"),
        vector_store=ChromaStore(
            persist_path=tmp_path / "chroma",
            collection_name="chunks",
        ),
        bm25_indexer=BM25Indexer(tmp_path / "bm25"),
        batch_size=2,
    )
    return (
        IngestionPipeline(
            corpus_processor=processor,
            indexing_pipeline=indexing,
            trace_collector=TraceCollector(tmp_path / "logs" / "images.jsonl"),
        ),
        image_storage,
        history_path,
    )


def test_interactive_ingestion_progress_trace_and_artifact_contract(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "SWL.I.11.01 Directed Putaway Configuration.pdf"
    _write_pdf(source)
    processed = tmp_path / "processed"
    history_path = tmp_path / "db" / "ingestion_history.db"
    processor = CorpusProcessor(
        source_root=staging,
        output_root=processed,
        database_path=history_path,
        splitter_settings=SplitterSettings(
            provider="recursive",
            chunk_size=80,
            chunk_overlap=10,
        ),
        transforms=(IdentityTransform(),),
        loader_builder=lambda *args, **kwargs: FixtureLoader(),
    )
    indexing = IndexingPipeline(
        embedding=LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models"),
        vector_store=ChromaStore(
            persist_path=tmp_path / "chroma",
            collection_name="chunks",
        ),
        bm25_indexer=BM25Indexer(tmp_path / "bm25"),
        batch_size=2,
    )
    trace_path = tmp_path / "logs" / "traces.jsonl"
    pipeline = IngestionPipeline(
        corpus_processor=processor,
        indexing_pipeline=indexing,
        trace_collector=TraceCollector(trace_path),
    )
    progress: list[tuple[str, int, int]] = []

    result = pipeline.run(
        source,
        collection="dashboard-test",
        on_progress=lambda *event: progress.append(event),
    )

    assert {stage for stage, _, _ in progress} == {
        "load",
        "split",
        "transform",
        "embed",
        "upsert",
    }
    assert progress[-1] == ("upsert", 2, 2)
    assert result.collection == "dashboard-test"
    assert Path(result.document_artifact_path).is_file()
    assert all(Path(path).is_file() for path in result.chunk_artifact_paths)
    assert result.indexing.vector_count == result.indexing.bm25_count
    assert result.indexing.vector_count >= 4

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    stages = {stage["name"]: stage for stage in trace["stages"]}
    for stage_name in ("load", "split", "transform", "embed", "upsert"):
        assert stage_name in stages
        assert "method" in stages[stage_name]["details"]
        assert "provider" in stages[stage_name]["details"]
    serialized_trace = json.dumps(trace, ensure_ascii=False)
    assert "Directed putaway configuration selects storage locations" not in serialized_trace
    assert "prompt" not in serialized_trace.casefold()

    records = SQLiteIntegrityChecker(history_path).list_processed(collection="dashboard-test")
    assert len(records) == 1
    metadata = records[0].metadata
    assert metadata["staged_pdf_path"] == str(source.resolve())
    assert metadata["document_artifact_path"] == result.document_artifact_path
    assert metadata["chunk_artifact_paths"] == list(result.chunk_artifact_paths)

    second_collection = pipeline.run(source, collection="dashboard-secondary")
    assert second_collection.document_id != result.document_id
    assert second_collection.indexing.vector_count == result.indexing.vector_count * 2
    assert len(SQLiteIntegrityChecker(history_path).list_processed(status="success")) == 2

    unchanged = pipeline.run(source, collection="dashboard-secondary")
    assert unchanged.skipped is True
    assert unchanged.indexing.dense_upserted == 0


def test_same_source_content_update_replaces_old_artifacts_history_and_indexes(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "SWL.I.11.01 Directed Putaway Configuration.pdf"
    _write_pdf(source, "v1")
    pipeline, history_path = _fixture_pipeline(tmp_path, staging)

    first = pipeline.run(source, collection="dashboard-test")
    old_document = Path(first.document_artifact_path)
    old_chunks = Path(first.chunk_artifact_paths[0])
    old_ids = set(pipeline.indexing_pipeline.vector_store.list_ids())
    first_hash = (
        SQLiteIntegrityChecker(history_path)
        .list_processed(collection="dashboard-test")[0]
        .file_hash
    )

    _write_pdf(source, "v2")
    second = pipeline.run(source, collection="dashboard-test")
    new_ids = set(pipeline.indexing_pipeline.vector_store.list_ids())

    assert second.document_id != first.document_id
    assert not old_document.exists()
    assert not old_chunks.exists()
    assert old_ids.isdisjoint(new_ids)
    assert new_ids == set(pipeline.indexing_pipeline.bm25_indexer.documents)
    records = SQLiteIntegrityChecker(history_path).list_processed(collection="dashboard-test")
    assert len(records) == 1
    assert records[0].file_hash != first_hash
    assert records[0].metadata["document_artifact_path"] == second.document_artifact_path


def test_history_commit_failure_rolls_back_all_core_stores_and_current_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "SWL.I.11.01 Directed Putaway Configuration.pdf"
    _write_pdf(source, "v1")
    pipeline, history_path = _fixture_pipeline(tmp_path, staging, trace_name="rollback.jsonl")
    first = pipeline.run(source, collection="dashboard-test")
    old_dense = set(pipeline.indexing_pipeline.vector_store.list_ids())
    old_sparse = set(pipeline.indexing_pipeline.bm25_indexer.documents)
    old_model = pipeline.indexing_pipeline.embedding.model_path.read_bytes()
    old_history = SQLiteIntegrityChecker(history_path).list_processed(collection="dashboard-test")
    original_mark_success = pipeline.corpus_processor.integrity.mark_success

    def commit_then_fail(*args, **kwargs) -> None:
        original_mark_success(*args, **kwargs)
        raise RuntimeError("injected history checkpoint failure")

    monkeypatch.setattr(
        pipeline.corpus_processor.integrity,
        "mark_success",
        commit_then_fail,
    )
    _write_pdf(source, "v2")

    with pytest.raises(RuntimeError, match="injected history"):
        pipeline.run(source, collection="dashboard-test")

    assert set(pipeline.indexing_pipeline.vector_store.list_ids()) == old_dense
    assert set(pipeline.indexing_pipeline.bm25_indexer.documents) == old_sparse
    assert pipeline.indexing_pipeline.embedding.model_path.read_bytes() == old_model
    assert Path(first.document_artifact_path).is_file()
    assert Path(first.chunk_artifact_paths[0]).is_file()
    assert len(list((tmp_path / "processed" / "documents").glob("*.json"))) == 1
    restored = SQLiteIntegrityChecker(history_path).list_processed(collection="dashboard-test")
    assert [(record.file_hash, record.status) for record in restored] == [
        (old_history[0].file_hash, "success")
    ]
    trace = json.loads(
        (tmp_path / "logs" / "rollback.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert trace["status"] == "error"
    assert any(stage["name"] == "history_commit_failure" for stage in trace["stages"])

    monkeypatch.setattr(
        pipeline.corpus_processor.integrity,
        "mark_success",
        original_mark_success,
    )
    retried = pipeline.run(source, collection="dashboard-test")
    unchanged = pipeline.run(source, collection="dashboard-test")
    assert retried.skipped is False
    assert unchanged.skipped is True


def test_legacy_bootstrap_is_preserved_across_two_ingestions_and_excludes_orphan(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    first_source = staging / "SWL.I.11.01 First.pdf"
    second_source = staging / "SWL.I.11.02 Second.pdf"
    _write_pdf(first_source, "new-first")
    _write_pdf(second_source, "new-second")
    pipeline, history_path = _fixture_pipeline(tmp_path, staging)
    chunks_dir = tmp_path / "processed" / "chunks"
    chunks_dir.mkdir(parents=True)

    legacy_chunks = [
        Chunk(
            id=f"legacy-{index}",
            text=f"Legacy warehouse configuration topic {index}",
            metadata={
                "collection": "legacy",
                "source_path": "legacy.pdf",
                "source_relative_path": "legacy.pdf",
                "file_hash": "legacy-hash",
            },
            start_offset=0,
            end_offset=40,
        )
        for index in range(4)
    ]
    old_same_source = Chunk(
        id="old-same-source",
        text="Obsolete directed putaway settings",
        metadata={
            "collection": "dashboard-test",
            "source_path": first_source.as_posix(),
            "source_relative_path": first_source.name,
            "file_hash": "old-file-hash",
        },
        start_offset=0,
        end_offset=36,
    )
    orphan = Chunk(
        id="disk-orphan",
        text="Never committed orphan artifact",
        metadata={
            "collection": "orphan",
            "source_path": "orphan.pdf",
            "source_relative_path": "orphan.pdf",
            "file_hash": "orphan-hash",
        },
        start_offset=0,
        end_offset=31,
    )
    (chunks_dir / "legacy.jsonl").write_text(
        "".join(json.dumps(chunk.to_dict()) + "\n" for chunk in [*legacy_chunks, old_same_source]),
        encoding="utf-8",
    )
    (chunks_dir / "orphan.jsonl").write_text(
        json.dumps(orphan.to_dict()) + "\n",
        encoding="utf-8",
    )
    pipeline.indexing_pipeline.index([*legacy_chunks, old_same_source])

    pipeline.run(first_source, collection="dashboard-test")
    after_first = set(pipeline.indexing_pipeline.vector_store.list_ids())
    pipeline.run(second_source, collection="dashboard-test")
    after_second = set(pipeline.indexing_pipeline.vector_store.list_ids())

    assert {chunk.id for chunk in legacy_chunks}.issubset(after_first)
    assert {chunk.id for chunk in legacy_chunks}.issubset(after_second)
    assert "old-same-source" not in after_first
    assert "disk-orphan" not in after_first | after_second
    assert len(SQLiteIntegrityChecker(history_path).list_processed(status="success")) == 2
    assert after_second == set(pipeline.indexing_pipeline.bm25_indexer.documents)


def test_image_lifecycle_survives_skip_failure_force_and_cross_collection_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "SWL.I.11.01 Image Configuration.pdf"
    _write_pdf(source, "image-v1")
    pipeline, image_storage, history_path = _image_fixture_pipeline(tmp_path, staging)

    pipeline.run(source, collection="collection-a")
    pipeline.run(source, collection="collection-b")
    unchanged = pipeline.run(source, collection="collection-a")
    first_records = SQLiteIntegrityChecker(history_path).list_processed(collection="collection-a")
    old_hash = first_records[0].file_hash
    old_raw = Path(first_records[0].metadata["extracted_image_artifact_paths"][0])
    images_a = image_storage.list_images(collection="collection-a", doc_hash=old_hash)
    images_b = image_storage.list_images(collection="collection-b", doc_hash=old_hash)

    assert unchanged.skipped is True
    assert old_raw.is_file()
    assert len(images_a) == len(images_b) == 1
    assert images_a[0].file_path.is_file()

    original_mark_success = pipeline.corpus_processor.integrity.mark_success

    def commit_then_fail(*args, **kwargs) -> None:
        original_mark_success(*args, **kwargs)
        raise RuntimeError("image history failure")

    monkeypatch.setattr(
        pipeline.corpus_processor.integrity,
        "mark_success",
        commit_then_fail,
    )
    _write_pdf(source, "image-v2-failed")
    with pytest.raises(RuntimeError, match="image history"):
        pipeline.run(source, collection="collection-a")

    assert image_storage.list_images(collection="collection-a", doc_hash=old_hash)
    assert old_raw.is_file()
    raw_files_after_failure = {
        path.resolve() for path in (tmp_path / "processed" / "images").rglob("*") if path.is_file()
    }
    assert raw_files_after_failure == {old_raw.resolve()}

    monkeypatch.setattr(
        pipeline.corpus_processor.integrity,
        "mark_success",
        original_mark_success,
    )
    _write_pdf(source, "image-v2")
    pipeline.run(source, collection="collection-a", force=True)
    new_record = SQLiteIntegrityChecker(history_path).list_processed(collection="collection-a")[0]
    new_images = image_storage.list_images(
        collection="collection-a",
        doc_hash=new_record.file_hash,
    )

    assert len(new_images) == 1
    assert new_images[0].file_path.is_file()
    assert image_storage.list_images(collection="collection-a", doc_hash=old_hash) == []
    assert image_storage.list_images(collection="collection-b", doc_hash=old_hash)
    assert old_raw.is_file()


def test_force_without_image_extraction_does_not_inherit_stale_image_paths(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "SWL.I.11.01 No Image Reprocess.pdf"
    _write_pdf(source, "no-image-reprocess")
    pipeline, _, history_path = _image_fixture_pipeline(tmp_path, staging)
    pipeline.run(source, collection="collection-a")

    record = SQLiteIntegrityChecker(history_path).list_processed(collection="collection-a")[0]
    old_paths = record.metadata.get("extracted_image_artifact_paths", [])
    assert len(old_paths) == 1

    no_image_pipeline, no_image_history = _fixture_pipeline(tmp_path, staging)
    no_image_pipeline.run(source, collection="collection-a", force=True)

    new_record = SQLiteIntegrityChecker(no_image_history).list_processed(
        collection="collection-a",
    )[0]
    inherited = new_record.metadata.get("extracted_image_artifact_paths", [])
    assert inherited == []
    chunks_path = Path(new_record.metadata["chunk_artifact_paths"][0])
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        chunk_data = json.loads(line)
        metadata = chunk_data.get("metadata", {})
        assert "images" not in metadata or not metadata["images"]


def test_concurrent_singleton_ingestions_serialize_full_corpus_resync(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    sources = [
        staging / "SWL.I.11.01 Concurrent First.pdf",
        staging / "SWL.I.11.02 Concurrent Second.pdf",
    ]
    for index, source in enumerate(sources):
        _write_pdf(source, f"concurrent-{index}")
    pipeline, history_path = _fixture_pipeline(tmp_path, staging)
    pipeline.trace_collector = None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda source: pipeline.run(source, collection="dashboard-test"),
                sources,
            )
        )

    assert len({result.document_id for result in results}) == 2
    assert len(SQLiteIntegrityChecker(history_path).list_processed(status="success")) == 2
    assert set(pipeline.indexing_pipeline.vector_store.list_ids()) == set(
        pipeline.indexing_pipeline.bm25_indexer.documents
    )
    assert pipeline.indexing_pipeline.vector_store.count() >= 8


def test_source_change_during_dense_encode_rolls_back_before_history_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "SWL.I.11.01 Mutable Source.pdf"
    _write_pdf(source, "source-v1")
    pipeline, history_path = _fixture_pipeline(tmp_path, staging, trace_name="source-race.jsonl")
    original_embed = pipeline.indexing_pipeline.embedding.embed
    mutated = False

    def mutate_source_during_embed(texts, trace=None):
        nonlocal mutated
        vectors = original_embed(texts, trace=trace)
        if not mutated:
            mutated = True
            _write_pdf(source, "source-v2")
        return vectors

    monkeypatch.setattr(
        pipeline.indexing_pipeline.embedding,
        "embed",
        mutate_source_during_embed,
    )

    with pytest.raises(RuntimeError, match="Source changed during ingestion"):
        pipeline.run(source, collection="dashboard-test")

    assert SQLiteIntegrityChecker(history_path).list_processed(status="success") == []
    assert pipeline.indexing_pipeline.vector_store.list_ids() == []
    assert pipeline.indexing_pipeline.bm25_indexer.count() == 0
    assert list((tmp_path / "processed" / "documents").glob("*.json")) == []
    assert list((tmp_path / "processed" / "chunks").glob("*.jsonl")) == []
    trace = json.loads(
        (tmp_path / "logs" / "source-race.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert any(stage["name"] == "history_commit_failure" for stage in trace["stages"])


def test_wal_journal_recovery_after_simulated_crash_preserves_committed_data(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "SWL.I.11.01 Crash Recovery.pdf"
    _write_pdf(source, "crash-recovery")
    pipeline, history_path = _fixture_pipeline(tmp_path, staging)
    pipeline.run(source, collection="collection-a")

    committed_before = SQLiteIntegrityChecker(history_path).list_processed(
        collection="collection-a",
    )
    assert len(committed_before) == 1

    leaked = sqlite3.connect(str(history_path))
    leaked.execute("SELECT COUNT(*) FROM ingestion_history")
    del leaked
    import gc

    gc.collect()

    recovered = SQLiteIntegrityChecker(history_path).list_processed(
        collection="collection-a",
    )
    assert len(recovered) == 1
    assert recovered[0].file_hash == committed_before[0].file_hash
    assert recovered[0].status == "success"

    assert pipeline.indexing_pipeline.vector_store.list_ids()
    assert pipeline.indexing_pipeline.bm25_indexer.count() > 0


def test_orphaned_artifacts_from_crashed_run_do_not_corrupt_restart(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "SWL.I.11.01 Orphan Recovery.pdf"
    _write_pdf(source, "orphan-recovery")
    pipeline, history_path = _fixture_pipeline(tmp_path, staging)
    pipeline.run(source, collection="collection-a")

    processed_dir = tmp_path / "processed"
    orphan_doc = processed_dir / "documents" / "orphan-crash-doc.json"
    orphan_chunk = processed_dir / "chunks" / "orphan-crash-doc.jsonl"
    orphan_doc.write_text(json.dumps({"id": "orphan", "text": "leftover"}))
    orphan_chunk.write_text(json.dumps({"id": "orphan-c1", "text": "leftover"}) + "\n")

    source2 = staging / "SWL.O.03.01 Orphan Restart.pdf"
    _write_pdf(source2, "orphan-restart")
    pipeline.run(source2, collection="collection-a")

    records = SQLiteIntegrityChecker(history_path).list_processed(collection="collection-a")
    assert len(records) == 2
    for record in records:
        assert record.status == "success"

    vector_ids = set(pipeline.indexing_pipeline.vector_store.list_ids())
    doc_chunks = []
    for record in records:
        chunk_path = Path(record.metadata["chunk_artifact_paths"][0])
        for line in chunk_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                chunk_data = json.loads(line)
                doc_chunks.append(chunk_data["id"])
                assert chunk_data["id"] in vector_ids
    assert len(doc_chunks) > 0
    bm25_count = pipeline.indexing_pipeline.bm25_indexer.count()
    assert bm25_count == len(vector_ids)
