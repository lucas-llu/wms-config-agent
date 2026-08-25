"""Verify that Dashboard management reads do not mutate configured local stores."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from observability.dashboard.services import ConfigService, get_dashboard_services


@dataclass(frozen=True, slots=True)
class NodeFingerprint:
    kind: str
    size: int | None
    modified_ns: int | None
    sha256: str | None


def configured_storage_paths(settings_path: str | Path) -> dict[str, Path]:
    """Resolve Dashboard storage roots without constructing any storage adapter."""
    settings = ConfigService.from_path(settings_path).settings
    return {
        "chroma": settings.vector_store.persist_path,
        "bm25": Path(os.getenv("WMS_BM25_PATH", "data/db/bm25")),
        "images": settings.ingestion.image_storage.root_path,
        "image_database": settings.ingestion.image_storage.database_path,
        "ingestion_history": Path(
            os.getenv("WMS_INGESTION_HISTORY_PATH", "data/db/ingestion_history.db")
        ),
    }


def snapshot_storage(paths: dict[str, Path]) -> dict[str, NodeFingerprint]:
    """Fingerprint files and directories, including missing configured roots."""
    snapshot: dict[str, NodeFingerprint] = {}
    for label, configured_path in sorted(paths.items()):
        root = configured_path.resolve()
        if not root.exists():
            snapshot[label] = _missing_fingerprint()
            _snapshot_parent_chain(snapshot, label, root)
            _snapshot_sqlite_sidecars(snapshot, label, root)
            continue
        snapshot[label] = _fingerprint(root)
        if root.is_dir():
            for child in sorted(root.rglob("*")):
                relative = child.relative_to(root).as_posix()
                snapshot[f"{label}/{relative}"] = _fingerprint(child)
        else:
            _snapshot_sqlite_sidecars(snapshot, label, root)
    return snapshot


def changed_nodes(
    before: dict[str, NodeFingerprint], after: dict[str, NodeFingerprint]
) -> list[str]:
    """Return stable logical node names whose fingerprints changed."""
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def exercise_dashboard_reads(settings_path: str | Path) -> dict[str, int]:
    """Exercise every Day 8 data read without exposing document content or paths."""
    get_dashboard_services.cache_clear()
    services = get_dashboard_services(str(settings_path))
    documents = services.data.list_documents()
    collections = services.data.list_collections()
    stats = services.data.get_collection_stats()
    detail_chunks = 0
    previewable_images = 0
    if documents:
        detail = services.data.get_document_detail(documents[0].doc_id)
        detail_chunks = len(services.data.chunk_rows(detail))
        previewable_images = len(services.data.previewable_images(detail))
    get_dashboard_services.cache_clear()
    return {
        "collections": len(collections),
        "documents": len(documents),
        "dense_chunks": stats.chunk_count,
        "sparse_chunks": stats.sparse_chunk_count,
        "images": stats.image_count,
        "sample_detail_chunks": detail_chunks,
        "sample_previewable_images": previewable_images,
    }


def verify_dashboard_reads(settings_path: str | Path) -> dict[str, int]:
    paths = configured_storage_paths(settings_path)
    before = snapshot_storage(paths)
    summary = exercise_dashboard_reads(settings_path)
    after = snapshot_storage(paths)
    changes = changed_nodes(before, after)
    if changes:
        raise RuntimeError(f"Dashboard reads modified storage nodes: {', '.join(changes)}")
    return summary


def parse_args(arguments: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    return parser.parse_args(arguments)


def main() -> None:
    args = parse_args()
    summary = verify_dashboard_reads(args.settings)
    print(json.dumps({"read_only": True, "summary": summary}, sort_keys=True))


def _fingerprint(path: Path) -> NodeFingerprint:
    stats = path.stat()
    if path.is_dir():
        return NodeFingerprint("directory", None, stats.st_mtime_ns, None)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return NodeFingerprint("file", stats.st_size, stats.st_mtime_ns, digest.hexdigest())


def _missing_fingerprint() -> NodeFingerprint:
    return NodeFingerprint("missing", None, None, None)


def _snapshot_sqlite_sidecars(
    snapshot: dict[str, NodeFingerprint],
    label: str,
    database_path: Path,
) -> None:
    for suffix in ("-journal", "-shm", "-wal"):
        sidecar = Path(f"{database_path}{suffix}")
        snapshot[f"{label}{suffix}"] = (
            _fingerprint(sidecar) if sidecar.exists() else _missing_fingerprint()
        )


def _snapshot_parent_chain(
    snapshot: dict[str, NodeFingerprint],
    label: str,
    missing_path: Path,
) -> None:
    """Record missing parents through the nearest existing directory.

    This catches a supposedly read-only adapter that initializes only a parent directory while
    leaving the configured database or index itself absent.
    """

    parent = missing_path.parent
    depth = 0
    while True:
        snapshot[f"{label}::parent:{depth}"] = (
            _fingerprint(parent) if parent.exists() else _missing_fingerprint()
        )
        if parent.exists() or parent == parent.parent:
            return
        parent = parent.parent
        depth += 1


if __name__ == "__main__":
    main()
