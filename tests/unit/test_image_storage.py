from __future__ import annotations

import sqlite3
from pathlib import Path

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


def test_store_metadata_images_updates_paths_without_mutating_input(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"image-data")
    storage = ImageStorage(tmp_path / "stored", tmp_path / "index.db")
    images = [{"id": "img-1", "path": str(source), "page": 1}]

    output = storage.store_metadata_images(
        images, collection="manuals", doc_hash="abc"
    )

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
