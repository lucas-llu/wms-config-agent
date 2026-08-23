"""PDF text and best-effort image extraction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pypdf import PdfReader

from core.types import Document
from libs.loader.base_loader import BaseLoader, DomainMetadata
from observability.logger import get_logger

logger = get_logger(__name__)

_SAFE_IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jp2",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


class PdfLoader(BaseLoader):
    """Load PDF pages and save extractable images without blocking text parsing."""

    allowed_extensions = {".pdf"}

    def __init__(self, image_output_dir: str | Path = "data/images") -> None:
        self.image_output_dir = Path(image_output_dir)

    def load(self, path: str | Path, metadata: DomainMetadata | None = None) -> Document:
        file_path = self.validate_path(path, self.allowed_extensions)
        file_hash = self.compute_file_hash(file_path)
        reader = PdfReader(file_path)

        document_text = ""
        pages: list[dict[str, int]] = []
        images: list[dict[str, Any]] = []

        for page_number, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            placeholders, page_images = self._extract_page_images(
                page=page,
                page_number=page_number,
                file_hash=file_hash,
            )
            page_content = page_text.strip()
            if placeholders:
                page_content = "\n".join(part for part in (page_content, *placeholders) if part)

            if document_text:
                document_text += "\n\n"
            page_start = len(document_text)
            document_text += page_content
            page_end = len(document_text)
            pages.append(
                {"page": page_number, "start_offset": page_start, "end_offset": page_end}
            )

            for image, placeholder in zip(page_images, placeholders, strict=True):
                local_offset = page_content.find(placeholder)
                image["text_offset"] = page_start + max(local_offset, 0)
                image["text_length"] = len(placeholder)
                images.append(image)

        document_metadata = self.build_metadata(
            file_path,
            doc_type="pdf",
            metadata=metadata,
            file_hash=file_hash,
        )
        document_metadata.update({"page_count": len(pages), "pages": pages, "images": images})
        return Document(
            id=self.build_document_id(file_hash),
            text=document_text,
            metadata=document_metadata,
        )

    def _extract_page_images(
        self,
        *,
        page: Any,
        page_number: int,
        file_hash: str,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        placeholders: list[str] = []
        extracted: list[dict[str, Any]] = []
        try:
            image_names = list(page.images.keys())
        except Exception as exc:  # pypdf intentionally exposes broken objects lazily
            logger.warning("Unable to inspect PDF images on page %s: %s", page_number, exc)
            return placeholders, extracted

        for sequence, image_name in enumerate(image_names, start=1):
            try:
                image_file = page.images[image_name]
                image_id = f"{file_hash}_{page_number}_{sequence}"
                suffix = Path(str(image_file.name)).suffix.lower()
                if suffix not in _SAFE_IMAGE_EXTENSIONS:
                    suffix = ".bin"
                output_dir = self.image_output_dir / file_hash
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = output_dir / f"{image_id}{suffix}"
                output_path.write_bytes(image_file.data)
                placeholder = f"[IMAGE: {image_id}]"
                placeholders.append(placeholder)
                extracted.append(
                    {
                        "id": image_id,
                        "path": output_path.as_posix(),
                        "page": page_number,
                        "text_offset": 0,
                        "text_length": len(placeholder),
                        "position": {"placement": "page_end"},
                    }
                )
            except Exception as exc:
                logger.warning(
                    "Unable to extract PDF image %s on page %s: %s",
                    image_name,
                    page_number,
                    exc,
                )
        return placeholders, extracted
