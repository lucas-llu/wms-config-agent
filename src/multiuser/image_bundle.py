"""Offline, scope-limited relocation of content-addressed evidence images.

No source mutation and no live cutover. The operator must stop source writers
and choose the approved collections and private destination before applying.
"""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ingestion.storage import ImageStorage

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})


@dataclass(frozen=True)
class PlannedImage:
    image_id: str
    collection: str
    doc_hash: str | None
    page_num: int | None
    created_at: str
    source_path: Path
    sha256: str
    size: int


def _hash(path):
    with path.open("rb") as content:
        return hashlib.file_digest(content, "sha256").hexdigest()


def plan_image_bundle(source_index, source_root, allowed_collections, *, max_bytes=5_000_000):
    """Inspect only explicitly approved collections; never print source paths."""
    index = Path(source_index).resolve(strict=True)
    root = Path(source_root).resolve(strict=True)
    if not index.is_file() or not root.is_dir():
        raise ValueError("Existing image index and root required")
    if (
        not isinstance(allowed_collections, (set, frozenset, tuple))
        or not allowed_collections
        or any(not isinstance(value, str) or not value.strip() for value in allowed_collections)
    ):
        raise ValueError("Explicit non-empty image collection allowlist required")
    allowed = frozenset(allowed_collections)
    if len(allowed) != len(allowed_collections):
        raise ValueError("Duplicate collection policy")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 5_000_000:
        raise ValueError("Bounded image size required")
    before = _hash(index)
    with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Source image index integrity failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Source image index references failed")
    records = ImageStorage(root, index, read_only=True).list_images()
    selected = []
    for record in records:
        if record.collection not in allowed:
            continue
        raw = record.file_path
        # Reject other volumes, traversal and UNC paths before resolving them.
        if not raw.is_absolute() or ".." in raw.parts or not raw.is_relative_to(root):
            raise ValueError("Indexed image escaped the approved source root")
        if raw.is_symlink():
            raise ValueError("Indexed image symlink is not allowed")
        path = raw.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Indexed image path is unavailable or out of scope")
        if path.suffix.lower() not in _SUFFIXES or not _DIGEST.fullmatch(path.stem):
            raise ValueError("Indexed image has an unsupported content address")
        size = path.stat().st_size
        if not 0 < size <= max_bytes:
            raise ValueError("Indexed image exceeds the displayable size bound")
        digest = _hash(path)
        if digest != path.stem:
            raise ValueError("Indexed image content checksum changed")
        if record.page_num is not None and record.page_num <= 0:
            raise ValueError("Indexed image page is invalid")
        selected.append(
            PlannedImage(
                record.image_id,
                record.collection,
                record.doc_hash,
                record.page_num,
                record.created_at,
                path,
                digest,
                size,
            )
        )
    if _hash(index) != before:
        raise ValueError("Source image index changed during planning")
    return {
        "source_index_sha256": before,
        "indexed": len(records),
        "selected": len(selected),
        "excluded": len(records) - len(selected),
        "distinct_files": len({image.source_path for image in selected}),
        "bytes": sum(
            image.size for image in {image.source_path: image for image in selected}.values()
        ),
        "records": tuple(selected),
    }


def stage_image_bundle(
    source_index,
    source_root,
    destination,
    allowed_collections,
    *,
    writers_stopped=False,
    private_destination_ready=False,
):
    """Create a new private bundle with re-rooted index paths; never overwrite."""
    if not writers_stopped or not private_destination_ready:
        raise PermissionError("Stopped source writers and an approved private destination required")
    target = Path(destination).resolve()
    if target.exists():
        raise ValueError("New image bundle destination required")
    planned = plan_image_bundle(source_index, source_root, allowed_collections)
    target.mkdir(parents=True, mode=0o700)
    storage = ImageStorage(target / "images", target / "image_index.db")
    created = []
    for image in planned["records"]:
        if _hash(image.source_path) != image.sha256:
            raise ValueError("Source image changed after planning")
        destination_path = storage.save_bytes(
            image.image_id,
            image.source_path.read_bytes(),
            collection=image.collection,
            extension=image.source_path.suffix,
            doc_hash=image.doc_hash,
            page_num=image.page_num,
        )
        if _hash(destination_path) != image.sha256:
            raise ValueError("Copied image checksum mismatch")
        created.append((image, destination_path))
    with closing(sqlite3.connect(storage.database_path)) as connection, connection:
        for image, _ in created:
            connection.execute(
                "UPDATE image_index SET created_at=? WHERE collection=? AND image_id=?",
                (image.created_at, image.collection, image.image_id),
            )
    copied = storage.list_images()
    if len(copied) != planned["selected"]:
        raise ValueError("Relocated index count mismatch")
    for record, (source, destination_path) in zip(copied, created, strict=True):
        if (
            record.image_id != source.image_id
            or record.collection != source.collection
            or record.doc_hash != source.doc_hash
            or record.page_num != source.page_num
            or record.created_at != source.created_at
            or record.file_path.resolve() != destination_path.resolve()
        ):
            raise ValueError("Relocated image metadata mismatch")
    if _hash(Path(source_index)) != planned["source_index_sha256"]:
        raise ValueError("Source image index changed during copying")
    summary = {key: value for key, value in planned.items() if key != "records"}
    summary.update(
        bundle_version=1,
        collections=sorted(frozenset(allowed_collections)),
        status="staged_not_activated",
        destination_index_sha256=_hash(storage.database_path),
        created_files=len({path for _, path in created}),
    )
    (target / "manifest.json").write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
    return summary


def verify_staged_image_bundle(bundle):
    """Read-only integrity check at the bundle's final absolute location."""
    path = Path(bundle)
    if path.is_symlink():
        raise ValueError("Image bundle directory may not be a symlink")
    path = path.resolve(strict=True)
    if not path.is_dir() or {item.name for item in path.iterdir()} != {
        "manifest.json", "image_index.db", "images"
    }:
        raise ValueError("Unexpected image bundle layout")
    manifest_file = path / "manifest.json"
    index = path / "image_index.db"
    images = path / "images"
    if any(item.is_symlink() for item in (manifest_file, index, images)) or not (
        manifest_file.is_file() and index.is_file() and images.is_dir()
    ):
        raise ValueError("Unsafe image bundle component")
    before = _hash(manifest_file)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    expected_fields = {
        "bundle_version", "collections", "status", "source_index_sha256",
        "destination_index_sha256", "indexed", "selected", "excluded",
        "distinct_files", "created_files", "bytes",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_fields:
        raise ValueError("Unsupported image bundle manifest")
    collections = manifest["collections"]
    if (
        type(manifest["bundle_version"]) is not int
        or manifest["bundle_version"] != 1
        or manifest["status"] != "staged_not_activated"
        or not isinstance(collections, list)
        or not collections
        or any(not isinstance(value, str) or not value.strip() for value in collections)
        or collections != sorted(set(collections))
        or any(type(manifest[key]) is not int or manifest[key] < 0 for key in
               ("indexed", "selected", "excluded", "distinct_files", "created_files", "bytes"))
        or manifest["indexed"] != manifest["selected"] + manifest["excluded"]
        or manifest["created_files"] != manifest["distinct_files"]
        or any(not isinstance(manifest[key], str) or not _DIGEST.fullmatch(manifest[key])
               for key in ("source_index_sha256", "destination_index_sha256"))
    ):
        raise ValueError("Invalid image bundle manifest")
    if _hash(index) != manifest["destination_index_sha256"]:
        raise ValueError("Image bundle index checksum changed")
    planned = plan_image_bundle(index, images, tuple(collections))
    if (
        planned["indexed"] != manifest["selected"]
        or planned["excluded"] != 0
        or planned["selected"] != manifest["selected"]
        or planned["distinct_files"] != manifest["distinct_files"]
        or planned["bytes"] != manifest["bytes"]
    ):
        raise ValueError("Image bundle index or file counts changed")
    indexed_files = {item.source_path for item in planned["records"]}
    actual_files = set()
    for item in images.rglob("*"):
        if item.is_symlink():
            raise ValueError("Image bundle contains a symlink")
        if item.is_file():
            actual_files.add(item.resolve(strict=True))
        elif not item.is_dir():
            raise ValueError("Image bundle contains an unsupported entry")
    if actual_files != indexed_files or _hash(manifest_file) != before:
        raise ValueError("Image bundle files or manifest changed")
    return {
        "status": "verified_not_activated",
        "collections": collections,
        "selected": planned["selected"],
        "distinct_files": planned["distinct_files"],
        "bytes": planned["bytes"],
        "destination_index_sha256": planned["source_index_sha256"],
    }
