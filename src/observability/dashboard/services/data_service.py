"""Read models used by Dashboard data pages."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ingestion import CollectionStats, DocumentDetail, DocumentInfo, DocumentManager

_PREVIEW_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}


class DataService:
    def __init__(self, document_manager: DocumentManager, *, image_root: str | Path) -> None:
        self.document_manager = document_manager
        self.image_root = Path(image_root)

    def list_documents(self, collection: str | None = None) -> list[DocumentInfo]:
        return self.document_manager.list_documents(collection)

    def list_collections(self) -> list[str]:
        return sorted({item.collection for item in self.list_documents()})

    def get_document_detail(self, doc_id: str) -> DocumentDetail:
        return self.document_manager.get_document_detail(doc_id)

    def get_collection_stats(self, collection: str | None = None) -> CollectionStats:
        return self.document_manager.get_collection_stats(collection)

    def document_rows(self, collection: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "Title": document.title or Path(document.source_path).name,
                "Collection": document.collection,
                "Chunks": document.chunk_count,
                "Images": document.image_count,
                "Ingested": document.ingested_at or "Unknown",
                "Source": document.source_path,
            }
            for document in self.list_documents(collection)
        ]

    def chunk_rows(self, detail: DocumentDetail) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, chunk in enumerate(detail.chunks, start=1):
            metadata = chunk.get("metadata", {})
            rows.append(
                {
                    "number": index,
                    "id": str(chunk.get("id", "")),
                    "text": str(chunk.get("text", "")),
                    "metadata": metadata if isinstance(metadata, dict) else {},
                }
            )
        return rows

    def previewable_images(self, detail: DocumentDetail) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        for image in detail.images:
            if not self._is_previewable(image.file_path):
                continue
            payload = asdict(image)
            payload["file_path"] = image.file_path
            images.append(payload)
        return images

    def _is_previewable(self, path: Path) -> bool:
        if path.suffix.lower() not in _PREVIEW_SUFFIXES or not path.is_file():
            return False
        try:
            path.resolve().relative_to(self.image_root.resolve())
        except ValueError:
            return False
        return True
