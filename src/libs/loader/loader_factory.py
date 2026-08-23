"""Select a local document loader by file extension."""

from __future__ import annotations

from pathlib import Path

from libs.loader.base_loader import BaseLoader
from libs.loader.pdf_loader import PdfLoader
from libs.loader.text_loader import TextLoader


class LoaderFactory:
    """Create a loader without exposing format-specific behavior to ingestion code."""

    @staticmethod
    def create(
        path: str | Path, *, image_output_dir: str | Path = "data/images"
    ) -> BaseLoader:
        suffix = Path(path).suffix.lower()
        if suffix == ".pdf":
            return PdfLoader(image_output_dir=image_output_dir)
        if suffix in TextLoader.allowed_extensions:
            return TextLoader()
        supported = ", ".join(sorted({".pdf", *TextLoader.allowed_extensions}))
        raise ValueError(f"Unsupported document type '{suffix}'; expected one of: {supported}")
