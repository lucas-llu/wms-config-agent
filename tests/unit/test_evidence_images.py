import io
import sqlite3

import pytest
from PIL import Image

from agents.workspace import Workspace
from core.evidence_text import clean_evidence_text, image_document_hash, image_references
from ingestion.storage import ImageStorage
from observability.dashboard.services.evidence_images import EvidenceImages

DOC = "a" * 64


def png():
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(stream, "PNG")
    return stream.getvalue()


def image_fixture(tmp_path):
    storage = ImageStorage(tmp_path / "images", tmp_path / "images.db")
    paths = {}
    for page in (13, 14, 15, 16, 17):
        paths[page] = storage.save_bytes(
            f"{DOC}_{page}_1",
            png(),
            collection="allowed",
            extension=".png",
            doc_hash=DOC,
            page_num=page,
        )
    reader = ImageStorage(storage.root_path, storage.database_path, read_only=True)
    resolver = EvidenceImages(reader, [storage.root_path])
    workspace = Workspace("workspace:legacy", "Legacy", (), (), (), ())
    evidence = {
        "collection": "allowed",
        "doc_hash": DOC,
        "page_start": 13,
        "page_end": 16,
        "excerpt": f"Scan the Slot. [IMAGE: {DOC}_14_1]",
    }
    return storage, resolver, workspace, evidence, paths


def test_markers_and_boundary_fragments_removed_without_losing_business_text():
    raw = f"83bfe73792_13_2] Scan the Slot. [IMAGE: {DOC}_14_1] Confirm deposit. [IMAGE:"
    assert clean_evidence_text(raw) == "Scan the Slot. Confirm deposit."
    assert (
        clean_evidence_text("Use SWL.I.99.01 and LPN Tracked = No.")
        == "Use SWL.I.99.01 and LPN Tracked = No."
    )
    assert image_references(raw) == (f"{DOC}_14_1",)
    assert image_document_hash(raw) == DOC


def test_page_images_resolved_readonly_and_not_beyond_cited_pages(tmp_path):
    storage, resolver, workspace, evidence, _ = image_fixture(tmp_path)
    before = storage.database_path.read_bytes()
    images, unavailable = resolver.resolve(evidence, workspace)
    assert [i["page"] for i in images] == [13, 14, 15, 16]
    assert all(i["page"] != 17 for i in images)
    assert unavailable is False
    assert before == storage.database_path.read_bytes()


def test_legacy_marker_hash_recovers_existing_images(tmp_path):
    _, resolver, workspace, evidence, _ = image_fixture(tmp_path)
    evidence.pop("doc_hash")
    assert resolver.resolve(evidence, workspace)[0]
    evidence["image_ids"] = None
    assert resolver.resolve(evidence, workspace)[0]


def test_restricted_workspace_requires_doc_binding_and_metadata_scope(tmp_path):
    _, resolver, _, evidence, _ = image_fixture(tmp_path)
    workspace = Workspace(
        "workspace:test", "Test", ("allowed",), ("inbound",), ("DC01",), ("test",)
    )
    evidence.update(module="inbound", site="DC01", environment="test")
    assert resolver.resolve(evidence, workspace)[0]
    assert resolver.resolve({**evidence, "collection": "foreign"}, workspace) == ([], True)
    assert resolver.resolve({**evidence, "module": "outbound"}, workspace) == ([], True)
    evidence.pop("doc_hash")
    assert resolver.resolve(evidence, workspace) == ([], True)


def test_paths_outside_roots_and_missing_or_oversized_images_are_not_served(tmp_path):
    storage, resolver, workspace, evidence, paths = image_fixture(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    with sqlite3.connect(storage.database_path) as db:
        db.execute("UPDATE image_index SET file_path = ?", (str(outside),))
    assert resolver.resolve(evidence, workspace) == ([], True)
    with sqlite3.connect(storage.database_path) as db:
        db.execute("UPDATE image_index SET file_path = ?", (str(paths[13]),))
    assert EvidenceImages(resolver.storage, resolver.roots, max_bytes=1).resolve(
        evidence, workspace
    ) == ([], True)
    paths[13].unlink()
    assert resolver.resolve(evidence, workspace) == ([], True)


def test_empty_unbound_or_broken_index_has_no_file_access(tmp_path):
    storage, resolver, workspace, evidence, _ = image_fixture(tmp_path)
    assert resolver.resolve({"excerpt": "ordinary text"}, workspace) == ([], False)
    assert resolver.resolve({**evidence, "collection": None}, workspace) == ([], True)
    assert resolver.resolve({**evidence, "doc_hash": "other"}, workspace) == ([], True)
    with pytest.raises(ValueError):
        EvidenceImages(storage, [storage.root_path])
    storage.database_path.write_bytes(b"not sqlite")
    assert resolver.resolve(evidence, workspace) == ([], True)
