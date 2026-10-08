import io
import sqlite3
from contextlib import closing

import pytest
from PIL import Image

from ingestion.storage import ImageStorage
from multiuser.image_bundle import plan_image_bundle, stage_image_bundle


def png(color):
    image = Image.new("RGB", (2, 2), color)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def source(tmp_path):
    root = tmp_path / "source" / "images"
    index = tmp_path / "source" / "image_index.db"
    storage = ImageStorage(root, index)
    storage.save_bytes(
        "a_1", png("red"), collection="approved", extension=".png", doc_hash="a" * 64, page_num=1
    )
    storage.save_bytes(
        "a_2", png("blue"), collection="approved", extension=".png", doc_hash="a" * 64, page_num=2
    )
    storage.save_bytes(
        "b_1", png("green"), collection="other", extension=".png", doc_hash="b" * 64, page_num=1
    )
    return root, index


def test_private_staging_rewrites_paths_preserves_metadata_and_excludes_other_scopes(tmp_path):
    root, index = source(tmp_path)
    before = ImageStorage(root, index, read_only=True).list_images(collection="approved")
    destination = tmp_path / "private" / "bundle"
    plan = plan_image_bundle(index, root, ("approved",))
    assert (plan["indexed"], plan["selected"], plan["excluded"], plan["distinct_files"]) == (
        3,
        2,
        1,
        2,
    )
    assert not destination.exists()
    with pytest.raises(PermissionError):
        stage_image_bundle(index, root, destination, ("approved",))
    assert not destination.exists()
    result = stage_image_bundle(
        index,
        root,
        destination,
        ("approved",),
        writers_stopped=True,
        private_destination_ready=True,
    )
    assert result["status"] == "staged_not_activated"
    assert result["created_files"] == 2
    assert result["source_index_sha256"] == plan["source_index_sha256"]
    copied = ImageStorage(
        destination / "images", destination / "image_index.db", read_only=True
    ).list_images()
    assert [
        (item.image_id, item.collection, item.doc_hash, item.page_num, item.created_at)
        for item in copied
    ] == [
        (item.image_id, item.collection, item.doc_hash, item.page_num, item.created_at)
        for item in before
    ]
    assert all(
        item.file_path.resolve().is_relative_to((destination / "images").resolve())
        for item in copied
    )
    with Image.open(copied[0].file_path) as image:
        assert image.size == (2, 2)
    with pytest.raises(ValueError, match="New image bundle"):
        stage_image_bundle(
            index,
            root,
            destination,
            ("approved",),
            writers_stopped=True,
            private_destination_ready=True,
        )


def test_path_escape_and_changed_content_fail_before_any_copy(tmp_path):
    root, index = source(tmp_path)
    original = ImageStorage(root, index, read_only=True).get_path("a_1", collection="approved")
    assert original is not None
    outside = tmp_path / "outside.png"
    outside.write_bytes(png("yellow"))
    with closing(sqlite3.connect(index)) as connection, connection:
        connection.execute(
            "UPDATE image_index SET file_path=? WHERE collection='approved' AND image_id='a_1'",
            (str(outside),),
        )
    destination = tmp_path / "private" / "bundle"
    with pytest.raises(ValueError, match="escaped"):
        stage_image_bundle(
            index,
            root,
            destination,
            ("approved",),
            writers_stopped=True,
            private_destination_ready=True,
        )
    assert not destination.exists()
    with closing(sqlite3.connect(index)) as connection, connection:
        connection.execute(
            "UPDATE image_index SET file_path=? WHERE collection='approved' AND image_id='a_1'",
            (str(original),),
        )
    storage = ImageStorage(root, index, read_only=True)
    changed = storage.get_path("a_2", collection="approved")
    assert changed is not None
    changed.write_bytes(png("yellow"))
    with pytest.raises(ValueError, match="checksum"):
        plan_image_bundle(index, root, ("approved",))


def test_empty_or_ambiguous_collection_selection_is_refused(tmp_path):
    root, index = source(tmp_path)
    with pytest.raises(ValueError, match="allowlist"):
        plan_image_bundle(index, root, ())
    with pytest.raises(ValueError, match="Duplicate"):
        plan_image_bundle(index, root, ("approved", "approved"))
