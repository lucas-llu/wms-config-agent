from __future__ import annotations

import sqlite3

import pytest

from core.types import ChunkRecord
from libs.vector_store import ChromaStore

pytestmark = pytest.mark.integration


def _record(chunk_id: str, vector: list[float], *, domain: str = "Inbound") -> ChunkRecord:
    return ChunkRecord(
        id=chunk_id,
        text=f"Text for {chunk_id}",
        metadata={
            "source_path": f"{chunk_id}.pdf",
            "domain": domain,
            "pages": [1, 2],
        },
        dense_vector=vector,
    )


def test_chroma_upsert_query_filter_and_get(tmp_path) -> None:
    store = ChromaStore(persist_path=tmp_path, collection_name="test_chunks")
    records = [
        _record("putaway", [1.0, 0.0, 0.0]),
        _record("appointment", [0.8, 0.2, 0.0]),
        _record("outbound", [0.0, 1.0, 0.0], domain="Outbound"),
    ]

    store.upsert(records)
    store.upsert(records)

    assert store.count() == 3
    assert store.query([1.0, 0.0, 0.0], top_k=1)[0]["id"] == "putaway"
    assert store.query([0.0, 0.0, 0.0], top_k=3) == []
    filtered = store.query([0.0, 1.0, 0.0], top_k=3, filters={"domain": "Inbound"})
    assert {item["id"] for item in filtered} == {"putaway", "appointment"}
    restored = store.get_by_ids(["outbound", "missing"])
    assert restored[0]["metadata"]["pages"] == [1, 2]

    inbound = store.get_by_metadata({"domain": "Inbound"})
    assert {item["id"] for item in inbound} == {"putaway", "appointment"}
    stats = store.get_collection_stats()
    assert stats["chunk_count"] == 3
    assert stats["document_count"] == 3
    assert store.delete_by_metadata({"domain": "Outbound"}) == 1
    assert store.count() == 2


def test_chroma_strict_read_only_management_reads_do_not_mutate_store(tmp_path) -> None:
    writable = ChromaStore(persist_path=tmp_path, collection_name="test_chunks")
    writable.upsert([_record("putaway", [1.0, 0.0, 0.0])])
    before = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    store = ChromaStore(
        persist_path=tmp_path,
        collection_name="test_chunks",
        read_only=True,
    )

    assert store.count() == 1
    assert store.get_by_ids(["putaway"])[0]["text"] == "Text for putaway"
    assert store.get_collection_stats()["chunk_count"] == 1
    with pytest.raises(PermissionError, match="read-only"):
        store.upsert([])
    with pytest.raises(PermissionError, match="read-only"):
        store.delete([])
    with pytest.raises(PermissionError, match="read-only"):
        store.delete_by_metadata({"domain": "Inbound"})
    after = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_chroma_strict_read_only_missing_store_stays_missing(tmp_path) -> None:
    persist_path = tmp_path / "missing"

    store = ChromaStore(
        persist_path=persist_path,
        collection_name="test_chunks",
        read_only=True,
    )

    assert store.count() == 0
    assert store.get_by_metadata() == []
    assert not persist_path.exists()


def test_chroma_management_reader_reports_incompatible_schema(tmp_path) -> None:
    database = tmp_path / "chroma.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    store = ChromaStore(
        persist_path=tmp_path,
        collection_name="test_chunks",
        read_only=True,
    )

    with pytest.raises(RuntimeError, match=r"expected Chroma 1\.5\.x"):
        store.count()


def test_chroma_management_reader_releases_sqlite_handle(tmp_path) -> None:
    database = tmp_path / "chroma.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT, scope TEXT);
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY, segment_id TEXT, embedding_id TEXT
            );
            CREATE TABLE embedding_metadata (
                id INTEGER, key TEXT, string_value TEXT, int_value INTEGER,
                float_value REAL, bool_value INTEGER
            );
            INSERT INTO collections VALUES ('collection-id', 'test_chunks');
            INSERT INTO segments VALUES ('segment-id', 'collection-id', 'METADATA');
            INSERT INTO embeddings VALUES (1, 'segment-id', 'chunk-id');
            """
        )
        connection.commit()
    finally:
        connection.close()
    store = ChromaStore(
        persist_path=tmp_path,
        collection_name="test_chunks",
        read_only=True,
    )

    assert store.count() == 1
    moved = tmp_path / "chroma-moved.sqlite3"
    database.replace(moved)
    assert moved.is_file()
