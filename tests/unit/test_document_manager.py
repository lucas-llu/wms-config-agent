from __future__ import annotations

from pathlib import Path

import pytest

from core.types import Chunk, ChunkRecord
from ingestion import DocumentManager
from ingestion.embedding import SparseEncoder
from ingestion.storage import BM25Indexer, ImageStorage
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
def test_document_manager_rejects_unknown_detail_and_scopes_collection(tmp_path: Path) -> None:
    manager, _ = _build_manager(tmp_path)

    assert manager.list_documents("other") == []
    with pytest.raises(KeyError, match="Unknown document"):
        manager.get_document_detail("missing")
