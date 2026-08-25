from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from ingestion.storage import ImageStorage


def test_save_and_lookup_survive_new_storage_instance(tmp_path: Path) -> None:
    root = tmp_path / "images"
    database = tmp_path / "db" / "image_index.db"
    storage = ImageStorage(root, database)

    saved = storage.save_bytes(
        "img-1",
        b"png-bytes",
        collection="wms-system-training",
        extension=".png",
        doc_hash="doc-hash",
        page_num=4,
    )

    assert saved.is_file()
    assert saved.parent.name == "wms-system-training"
    assert ImageStorage(root, database).get_path("img-1") == saved
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT collection, doc_hash, page_num FROM image_index WHERE image_id = ?",
            ("img-1",),
        ).fetchone()
    assert row == ("wms-system-training", "doc-hash", 4)


def test_same_bytes_are_deduplicated_but_ids_remain_queryable(tmp_path: Path) -> None:
    storage = ImageStorage(tmp_path / "images", tmp_path / "index.db")

    first = storage.save_bytes("img-1", b"same", collection="manuals", extension=".jpg")
    second = storage.save_bytes("img-2", b"same", collection="manuals", extension=".jpg")

    assert first == second
    assert storage.count() == 2
    assert storage.list_collection("manuals") == {"img-1": first, "img-2": second}


def test_replacing_image_id_removes_unreferenced_previous_content(tmp_path: Path) -> None:
    storage = ImageStorage(tmp_path / "images", tmp_path / "index.db")
    previous = storage.save_bytes("image", b"old", collection="manuals", extension=".png")

    current = storage.save_bytes("image", b"new", collection="manuals", extension=".png")

    assert current != previous
    assert storage.get_path("image", collection="manuals") == current
    assert current.is_file()
    assert not previous.exists()
    assert storage.pending_cleanup_count() == 0


def test_same_image_id_is_namespaced_by_collection_and_deletes_independently(
    tmp_path: Path,
) -> None:
    storage = ImageStorage(tmp_path / "images", tmp_path / "index.db")

    first = storage.save_bytes(
        "shared-id", b"same", collection="manuals", extension=".png", doc_hash="same-hash"
    )
    second = storage.save_bytes(
        "shared-id", b"same", collection="training", extension=".png", doc_hash="same-hash"
    )

    assert storage.count() == 2
    assert storage.get_path("shared-id", collection="manuals") == first
    assert storage.get_path("shared-id", collection="training") == second
    with pytest.raises(ValueError, match="multiple collections"):
        storage.get_path("shared-id")
    assert storage.remove_document("same-hash", collection="manuals") == 1
    assert not first.exists()
    assert second.is_file()
    assert storage.list_collection("training") == {"shared-id": second}


def test_legacy_image_schema_migrates_to_collection_scoped_identity(tmp_path: Path) -> None:
    database = tmp_path / "index.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE image_index (
                image_id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                collection TEXT,
                doc_hash TEXT,
                page_num INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX idx_image_collection ON image_index(collection);
            CREATE INDEX idx_image_doc_hash ON image_index(doc_hash);
            INSERT INTO image_index (
                image_id, file_path, collection, doc_hash, page_num
            ) VALUES ('legacy', 'legacy.png', 'manuals', 'hash', 1);
            """
        )
        connection.commit()
    finally:
        connection.close()

    storage = ImageStorage(tmp_path / "images", database)

    assert storage.list_collection("manuals") == {"legacy": Path("legacy.png")}
    with sqlite3.connect(database) as connection:
        primary_key = [
            row[1]
            for row in sorted(
                connection.execute("PRAGMA table_info(image_index)"), key=lambda row: row[5]
            )
            if row[5]
        ]
    assert primary_key == ["collection", "image_id"]


def test_concurrent_storage_construction_serializes_schema_initialization(tmp_path: Path) -> None:
    database = tmp_path / "index.db"
    root = tmp_path / "images"

    with ThreadPoolExecutor(max_workers=16) as pool:
        stores = list(pool.map(lambda _: ImageStorage(root, database), range(32)))

    assert len(stores) == 32
    saved = stores[0].save_bytes("image", b"content", collection="manuals")
    assert stores[-1].get_path("image", collection="manuals") == saved


def test_store_metadata_images_updates_paths_without_mutating_input(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"image-data")
    storage = ImageStorage(tmp_path / "stored", tmp_path / "index.db")
    images = [{"id": "img-1", "path": str(source), "page": 1}]

    output = storage.store_metadata_images(images, collection="manuals", doc_hash="abc")

    assert output[0]["path"] != images[0]["path"]
    assert Path(output[0]["path"]).read_bytes() == b"image-data"
    assert images[0]["path"] == str(source)


def test_invalid_inputs_are_rejected(tmp_path: Path) -> None:
    storage = ImageStorage(tmp_path / "images", tmp_path / "index.db")

    try:
        storage.save_bytes("", b"data", collection="manuals")
    except ValueError as exc:
        assert "image_id" in str(exc)
    else:
        raise AssertionError("empty image ID should fail")


def test_list_and_remove_document_preserve_shared_content(tmp_path: Path) -> None:
    storage = ImageStorage(tmp_path / "images", tmp_path / "index.db")
    first = storage.save_bytes(
        "first", b"shared", collection="manuals", extension=".png", doc_hash="doc-1"
    )
    storage.save_bytes(
        "second", b"shared", collection="manuals", extension=".png", doc_hash="doc-2"
    )

    assert storage.list_images(collection="manuals", doc_hash="doc-1")[0].file_path == first
    assert storage.remove_document("doc-1", collection="manuals") == 1
    assert first.is_file()
    assert storage.remove_document("doc-2", collection="manuals") == 1
    assert not first.exists()


def test_remove_document_queues_failed_file_cleanup_without_restoring_broken_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "images"
    database = tmp_path / "db" / "images.db"
    storage = ImageStorage(root, database)
    first = storage.save_bytes(
        "first",
        b"first-image",
        collection="manuals",
        extension=".png",
        doc_hash="document",
    )
    second = storage.save_bytes(
        "second",
        b"second-image",
        collection="manuals",
        extension=".png",
        doc_hash="document",
    )
    original_unlink = Path.unlink
    managed_attempts = 0

    def fail_second_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal managed_attempts
        if path in {first, second}:
            managed_attempts += 1
            if managed_attempts == 2:
                raise PermissionError("injected image lock")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second_unlink)

    assert storage.remove_document("document", collection="manuals") == 2
    assert storage.list_images(collection="manuals", doc_hash="document") == []
    assert storage.pending_cleanup_count() == 1
    assert sum(path.is_file() for path in (first, second)) == 1

    monkeypatch.setattr(Path, "unlink", original_unlink)
    reopened = ImageStorage(root, database)
    assert reopened.pending_cleanup_count() == 0
    assert not first.exists()
    assert not second.exists()


def test_concurrent_save_reserves_file_before_delete_queue_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = ImageStorage(tmp_path / "images", tmp_path / "images.db")
    path = storage.save_bytes(
        "old",
        b"shared-content",
        collection="manuals",
        extension=".png",
        doc_hash="old-document",
    )
    original_unlink = Path.unlink

    def keep_queued_file(target: Path, *args, **kwargs) -> None:
        if target == path:
            raise PermissionError("injected image lock")
        original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", keep_queued_file)
    assert storage.remove_document("old-document", collection="manuals") == 1
    assert storage.pending_cleanup_count() == 1
    monkeypatch.setattr(Path, "unlink", original_unlink)

    save_has_database_lock = Event()
    allow_save = Event()
    original_upsert = storage._upsert

    def paused_upsert(connection: sqlite3.Connection, **kwargs) -> bool:
        save_has_database_lock.set()
        assert allow_save.wait(timeout=2)
        return original_upsert(connection, **kwargs)

    monkeypatch.setattr(storage, "_upsert", paused_upsert)
    with ThreadPoolExecutor(max_workers=2) as pool:
        save = pool.submit(
            storage.save_bytes,
            "new",
            b"shared-content",
            collection="manuals",
            extension=".png",
            doc_hash="new-document",
        )
        assert save_has_database_lock.wait(timeout=2)
        purge = pool.submit(storage._purge_delete_queue)
        allow_save.set()
        assert save.result(timeout=2) == path
        purge.result(timeout=2)

    assert storage.get_path("new", collection="manuals") == path
    assert path.is_file()
    assert storage.pending_cleanup_count() == 0


def test_read_only_storage_reads_without_creating_or_mutating_files(tmp_path: Path) -> None:
    root = tmp_path / "images"
    database = tmp_path / "db" / "image_index.db"
    writable = ImageStorage(root, database)
    saved = writable.save_bytes("image", b"content", collection="manuals", doc_hash="document")
    before = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    storage = ImageStorage(root, database, read_only=True)

    assert storage.get_path("image") == saved
    assert storage.count() == 1
    assert storage.list_images(collection="manuals")[0].doc_hash == "document"
    with pytest.raises(PermissionError, match="read-only"):
        storage.save_bytes("other", b"content", collection="manuals")
    with pytest.raises(PermissionError, match="read-only"):
        storage.remove_document("document", collection="manuals")
    after = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_read_only_storage_tolerates_missing_database_without_creating_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing-images"
    database = tmp_path / "missing-db" / "image_index.db"

    storage = ImageStorage(root, database, read_only=True)

    assert storage.count() == 0
    assert storage.list_images() == []
    assert storage.get_path("missing") is None
    assert not root.exists()
    assert not database.parent.exists()
