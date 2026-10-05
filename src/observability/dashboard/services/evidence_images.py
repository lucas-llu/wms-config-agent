"""Resolve document-page images through the read-only index and workspace scope."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from agents.workspace import Workspace
from core.evidence_text import image_document_hash, image_references
from ingestion.storage import ImageStorage

_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})


class EvidenceImages:
    def __init__(
        self, storage: ImageStorage, allowed_roots: list[str | Path], *, max_bytes=5_000_000
    ):
        if not storage.read_only:
            raise ValueError("Evidence image storage must be read-only")
        self.storage = storage
        self.roots = tuple(Path(root).resolve() for root in allowed_roots)
        self.max_bytes = max_bytes

    def resolve(self, evidence: dict[str, Any], workspace: Workspace) -> tuple[list[dict], bool]:
        refs = set(image_references(str(evidence.get("excerpt", ""))))
        refs.update(value for value in (evidence.get("image_ids") or []) if isinstance(value, str))
        doc_hash = evidence.get("doc_hash")
        if not refs and not doc_hash:
            return [], False
        if not workspace.permits_metadata(evidence):
            return [], True
        if not doc_hash and workspace.workspace_id == "workspace:legacy":
            doc_hash = image_document_hash(str(evidence.get("excerpt", "")))
        if not isinstance(doc_hash, str) or not doc_hash:
            return [], True
        collection = evidence.get("collection")
        if not isinstance(collection, str) or not collection:
            return [], True
        first, last = evidence.get("page_start"), evidence.get("page_end")
        first = (
            first if isinstance(first, int) and not isinstance(first, bool) and first > 0 else None
        )
        last = (
            last
            if isinstance(last, int) and not isinstance(last, bool) and last >= (first or 1)
            else first
        )
        images, seen, resolved_refs = [], set(), set()
        unavailable = False
        try:
            records = self.storage.list_images(collection=collection, doc_hash=doc_hash)
            for record in records:
                on_page = (
                    first is not None
                    and record.page_num is not None
                    and first <= record.page_num <= last
                )
                if first is not None and not on_page:
                    continue
                if record.image_id not in refs and not on_page:
                    continue
                path = record.file_path.resolve()
                if (
                    not any(path.is_relative_to(root) for root in self.roots)
                    or path.suffix.lower() not in _SUFFIXES
                    or not path.is_file()
                    or not 0 < path.stat().st_size <= self.max_bytes
                ):
                    unavailable = True
                    continue
                resolved_refs.add(record.image_id)
                identity = (path, record.page_num)
                if identity not in seen:
                    seen.add(identity)
                    ordinal = record.image_id.rsplit("_", 1)[-1]
                    images.append(
                        {
                            "path": path,
                            "page": record.page_num,
                            "order": int(ordinal) if ordinal.isdigit() else 0,
                        }
                    )
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return [], True
        images.sort(key=lambda image: (image["page"] or 0, image["order"], str(image["path"])))
        return images, unavailable or bool(refs - resolved_refs)
