"""Adapt text splitter output into traceable ingestion Chunk objects."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from core.settings import Settings, SplitterSettings
from core.types import Chunk, Document
from libs.splitter import BaseSplitter, SplitterFactory

_IMAGE_REFERENCE = re.compile(r"\[IMAGE:\s*([^\]]+?)\s*\]")


class DocumentChunker:
    """Add stable identities, inherited metadata, and source locations to text chunks."""

    def __init__(
        self,
        settings: Settings | SplitterSettings,
        *,
        splitter: BaseSplitter | None = None,
    ) -> None:
        self.splitter = splitter or SplitterFactory.create(settings)

    def split_document(self, document: Document) -> list[Chunk]:
        chunk_texts = self.splitter.split_text(document.text)
        chunks: list[Chunk] = []
        previous_end = 0
        overlap = int(getattr(self.splitter, "chunk_overlap", 0))

        for index, chunk_text in enumerate(chunk_texts):
            start_offset, end_offset = self._locate_offsets(
                document.text,
                chunk_text,
                previous_end=previous_end,
                overlap=overlap,
            )
            metadata = self._inherit_metadata(
                document,
                chunk_index=index,
                chunk_text=chunk_text,
                start_offset=start_offset,
                end_offset=end_offset,
            )
            chunks.append(
                Chunk(
                    id=self._generate_chunk_id(document.id, index, chunk_text),
                    text=chunk_text,
                    metadata=metadata,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    source_ref=document.id,
                )
            )
            previous_end = end_offset
        return chunks

    @staticmethod
    def _generate_chunk_id(doc_id: str, index: int, text: str) -> str:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        return f"{doc_id}_{index:04d}_{text_hash}"

    @staticmethod
    def _locate_offsets(
        document_text: str,
        chunk_text: str,
        *,
        previous_end: int,
        overlap: int,
    ) -> tuple[int, int]:
        search_start = max(0, previous_end - overlap - 2)
        start = document_text.find(chunk_text, search_start)
        if start < 0:
            start = document_text.find(chunk_text)
        if start < 0:
            raise ValueError("Splitter returned text that is not present in the source document")
        return start, start + len(chunk_text)

    @staticmethod
    def _inherit_metadata(
        document: Document,
        *,
        chunk_index: int,
        chunk_text: str,
        start_offset: int,
        end_offset: int,
    ) -> dict[str, Any]:
        metadata = copy.deepcopy(document.metadata)
        document_images = metadata.pop("images", [])
        metadata["chunk_index"] = chunk_index

        image_refs = list(dict.fromkeys(_IMAGE_REFERENCE.findall(chunk_text)))
        if image_refs:
            image_by_id = {image.get("id"): image for image in document_images}
            metadata["image_refs"] = image_refs
            metadata["images"] = [
                image_by_id[image_id] for image_id in image_refs if image_id in image_by_id
            ]

        pages = metadata.get("pages", [])
        matched_pages = [
            int(page["page"])
            for page in pages
            if start_offset < int(page["end_offset"]) and end_offset > int(page["start_offset"])
        ]
        if matched_pages:
            metadata["page_start"] = min(matched_pages)
            metadata["page_end"] = max(matched_pages)
        return metadata
