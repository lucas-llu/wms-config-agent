from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.types import Chunk, ChunkRecord
from ingestion import DocumentManager
from ingestion.embedding import SparseEncoder
from ingestion.pipeline import load_preprocessed_chunks
from ingestion.storage import BM25Indexer, ImageStorage, LocalArtifactStorage
from libs.loader import SQLiteIntegrityChecker
from libs.vector_store import ChromaStore


def _build_manager(tmp_path: Path) -> tuple[DocumentManager, Path]:
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"fixture-pdf")
    source_path = source.as_posix()
    file_hash = "fixture-hash"
    metadata = {
        "source_path": source_path,
        "source_relative_path": "manual.pdf",
        "collection": "manuals",
        "file_hash": file_hash,
        "title": "Fixture manual",
        "images": [],
    }
    records = [
        ChunkRecord("manual-1", "putaway policy", metadata, dense_vector=[1.0, 0.0]),
        ChunkRecord("manual-2", "allocation rule", metadata, dense_vector=[0.0, 1.0]),
    ]
    chroma = ChromaStore(persist_path=tmp_path / "chroma", collection_name="chunks")
    chroma.upsert(records)
    bm25 = BM25Indexer(tmp_path / "bm25")
    bm25.build(
        SparseEncoder().encode(
            [Chunk(record.id, record.text, metadata, 0, len(record.text)) for record in records]
        )
    )
    images = ImageStorage(tmp_path / "images", tmp_path / "images.db")
    images.save_bytes(
        "manual-image",
        b"png-data",
        collection="manuals",
        extension=".png",
        doc_hash=file_hash,
    )
    integrity = SQLiteIntegrityChecker(tmp_path / "history.db")
    integrity.mark_success(file_hash, source, collection="manuals", chunk_count=2)
    return DocumentManager(chroma, bm25, images, integrity), source


@pytest.mark.integration
def test_document_manager_lists_details_stats_and_coordinates_delete(tmp_path: Path) -> None:
    manager, source = _build_manager(tmp_path)

    documents = manager.list_documents("manuals")

    assert len(documents) == 1
    assert documents[0].title == "Fixture manual"
    assert documents[0].chunk_count == 2
    assert documents[0].image_count == 1
    assert documents[0].ingested_at
    detail = manager.get_document_detail(documents[0].doc_id)
    assert {chunk["id"] for chunk in detail.chunks} == {"manual-1", "manual-2"}
    assert detail.images[0].image_id == "manual-image"
    assert manager.get_collection_stats("manuals").chunk_count == 2

    result = manager.delete_document(source.as_posix(), "manuals")

    assert result.success
    assert result.dense_deleted == 2
    assert result.sparse_deleted == 2
    assert result.images_deleted == 1
    assert result.history_deleted == 1
    assert manager.list_documents("manuals") == []
    assert manager.bm25_indexer.count() == 0
    assert manager.image_storage.count() == 0
    assert manager.file_integrity.list_processed(status=None) == []


@pytest.mark.integration
def test_document_delete_waits_for_shared_lifecycle_lock_even_when_lock_is_old(
    tmp_path: Path,
) -> None:
    manager, source = _build_manager(tmp_path)
    assert manager.lifecycle_lock is not None
    owner = manager.lifecycle_lock.lease()
    manager.lifecycle_lock.timeout_seconds = 0.05
    manager.lifecycle_lock.poll_interval_seconds = 0.005

    with owner:
        os.utime(owner.path, (1, 1))
        with pytest.raises(TimeoutError, match="lifecycle lock"):
            manager.delete_document(source.as_posix(), "manuals")

    assert len(manager.list_documents("manuals")) == 1


@pytest.mark.integration
def test_document_delete_fails_closed_before_other_stores_when_generation_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, source = _build_manager(tmp_path)

    def fail_refresh() -> None:
        raise RuntimeError("injected stale generation")

    monkeypatch.setattr(manager.chroma_store, "refresh_active_generation", fail_refresh)

    with pytest.raises(RuntimeError, match="stale generation"):
        manager.delete_document(source.as_posix(), "manuals")

    assert manager.bm25_indexer.count() == 2
    assert manager.image_storage.count() == 1
    assert len(manager.file_integrity.list_processed(status=None)) == 1


@pytest.mark.integration
def test_document_manager_rejects_unknown_detail_and_scopes_collection(tmp_path: Path) -> None:
    manager, _ = _build_manager(tmp_path)

    assert manager.list_documents("other") == []
    with pytest.raises(KeyError, match="Unknown document"):
        manager.get_document_detail("missing")


@pytest.mark.integration
def test_document_identity_stats_and_delete_are_collection_scoped(tmp_path: Path) -> None:
    source = tmp_path / "shared.pdf"
    source.write_bytes(b"fixture")
    source_path = source.as_posix()
    records: list[ChunkRecord] = []
    for collection in ("manuals", "training"):
        metadata = {
            "source_path": source_path,
            "collection": collection,
            "file_hash": "same-hash",
            "title": f"{collection} copy",
        }
        records.append(
            ChunkRecord(
                f"{collection}-chunk",
                f"{collection} configuration",
                metadata,
                dense_vector=[1.0, 0.0],
            )
        )
    chroma = ChromaStore(persist_path=tmp_path / "chroma", collection_name="chunks")
    chroma.upsert(records)
    bm25 = BM25Indexer(tmp_path / "bm25")
    bm25.build(
        SparseEncoder().encode(
            [
                Chunk(record.id, record.text, record.metadata, 0, len(record.text))
                for record in records
            ]
        )
    )
    images = ImageStorage(tmp_path / "images", tmp_path / "images.db")
    for collection in ("manuals", "training"):
        images.save_bytes(
            f"{collection}-image",
            b"shared-image",
            collection=collection,
            doc_hash="same-hash",
        )
    integrity = SQLiteIntegrityChecker(tmp_path / "history.db")
    for collection in ("manuals", "training"):
        integrity.mark_success(
            "same-hash",
            source,
            collection=collection,
            staged_pdf_path=str(source),
        )
    manager = DocumentManager(
        chroma,
        bm25,
        images,
        integrity,
        LocalArtifactStorage([tmp_path]),
    )

    documents = manager.list_documents()

    assert len({document.doc_id for document in documents}) == 2
    assert {
        manager.get_document_detail(document.doc_id).document.collection for document in documents
    } == {"manuals", "training"}
    assert manager.get_collection_stats("manuals").sparse_chunk_count == 1
    assert manager.get_collection_stats("training").sparse_chunk_count == 1

    deleted = manager.delete_document(source_path, "manuals")

    assert deleted.success
    assert deleted.dense_deleted == deleted.sparse_deleted == deleted.images_deleted == 1
    assert deleted.history_deleted == 1
    assert [document.collection for document in manager.list_documents()] == ["training"]
    assert manager.get_collection_stats("training").sparse_chunk_count == 1
    assert len(integrity.list_processed(status=None, collection="training")) == 1
    assert source.is_file()
    repeated = manager.delete_document(source_path, "manuals")
    assert repeated.success
    assert (
        repeated.dense_deleted,
        repeated.sparse_deleted,
        repeated.images_deleted,
        repeated.history_deleted,
    ) == (0, 0, 0, 0)

    final = manager.delete_document(source_path, "training")
    assert final.success
    assert final.artifacts_deleted == 1
    assert not source.exists()


@pytest.mark.integration
def test_document_manager_removes_sparse_orphan_without_vector_records(tmp_path: Path) -> None:
    manager, source = _build_manager(tmp_path)
    manager.chroma_store.delete_by_metadata(
        {"source_path": source.as_posix(), "collection": "manuals"}
    )

    result = manager.delete_document(source.as_posix(), "manuals")

    assert result.success
    assert result.dense_deleted == 0
    assert result.sparse_deleted == 2
    assert manager.bm25_indexer.count() == 0


@pytest.mark.integration
def test_document_manager_preserves_external_sparse_update_from_stale_instance(
    tmp_path: Path,
) -> None:
    manager, source = _build_manager(tmp_path)
    external = BM25Indexer(tmp_path / "bm25")
    extra = Chunk(
        "external",
        "cycle count",
        {"source_path": "external.pdf", "collection": "manuals"},
        0,
        11,
    )
    external.upsert(SparseEncoder().encode([extra]))

    result = manager.delete_document(source.as_posix(), "manuals")

    assert result.sparse_deleted == 2
    assert BM25Indexer(tmp_path / "bm25").query("cycle count", top_k=1)[0]["chunk_id"] == (
        "external"
    )


@pytest.mark.integration
def test_partial_vector_failure_does_not_block_other_store_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, source = _build_manager(tmp_path)

    def fail_lookup(filters: dict[str, str] | None = None, **kwargs: object) -> list[object]:
        del filters, kwargs
        raise RuntimeError("vector unavailable")

    monkeypatch.setattr(manager.chroma_store, "get_by_metadata", fail_lookup)

    result = manager.delete_document(source.as_posix(), "manuals")

    assert not result.success
    assert result.sparse_deleted == 2
    assert result.images_deleted == 1
    assert result.history_deleted == 1
    assert any(error.startswith("vector lookup:") for error in result.errors)
    assert any(error.startswith("vector deletion:") for error in result.errors)


@pytest.mark.integration
def test_document_delete_removes_allowlisted_artifacts_before_history(tmp_path: Path) -> None:
    manager, source = _build_manager(tmp_path)
    staging = tmp_path / "staging"
    documents = tmp_path / "processed" / "documents"
    chunks = tmp_path / "processed" / "chunks"
    extracted_images = tmp_path / "processed" / "images"
    staging.mkdir()
    documents.mkdir(parents=True)
    chunks.mkdir(parents=True)
    extracted_images.mkdir(parents=True)
    staged_pdf = staging / "uploaded.pdf"
    document_artifact = documents / "manual.json"
    chunk_artifact = chunks / "manual.jsonl"
    retained_artifact = chunks / "retained.jsonl"
    extracted_image = extracted_images / "manual-page-1.png"
    staged_pdf.write_bytes(b"pdf")
    document_artifact.write_text("{}", encoding="utf-8")
    extracted_image.write_bytes(b"image")
    chunk_artifact.write_text(
        json.dumps(
            Chunk("deleted", "putaway", {"source_path": "deleted.pdf"}, 0, 7).to_dict(),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    retained_artifact.write_text(
        json.dumps(
            Chunk(
                "retained",
                "allocation",
                {"source_path": "retained.pdf"},
                0,
                10,
            ).to_dict(),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manager.file_integrity.mark_success(
        "fixture-hash",
        source.name,
        collection="manuals",
        source_relative_path=source.name,
        staged_pdf_path=str(staged_pdf),
        document_artifact_path=str(document_artifact),
        chunk_artifact_paths=[str(chunk_artifact)],
        extracted_image_artifact_paths=[str(extracted_image)],
    )
    manager = DocumentManager(
        manager.chroma_store,
        manager.bm25_indexer,
        manager.image_storage,
        manager.file_integrity,
        LocalArtifactStorage([staging, documents, chunks, extracted_images]),
    )

    result = manager.delete_document(source.as_posix(), "manuals")

    assert result.success
    assert result.artifacts_deleted == 4
    assert not staged_pdf.exists()
    assert not document_artifact.exists()
    assert not extracted_image.exists()
    assert [chunk.id for chunk in load_preprocessed_chunks(chunks)] == ["retained"]
    assert manager.file_integrity.list_processed(status=None) == []
