"""UTF-8 Markdown and plain-text document loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.types import Document
from libs.loader.base_loader import BaseLoader, DomainMetadata


class TextLoader(BaseLoader):
    """Load Markdown or text, including optional Markdown YAML front matter."""

    allowed_extensions = {".md", ".markdown", ".txt"}

    def load(self, path: str | Path, metadata: DomainMetadata | None = None) -> Document:
        file_path = self.validate_path(path, self.allowed_extensions)
        raw_text = file_path.read_text(encoding="utf-8-sig")
        front_matter, text = self._parse_front_matter(raw_text, file_path)
        combined_metadata: dict[str, Any] = dict(front_matter)
        if metadata:
            combined_metadata.update(metadata)

        file_hash = self.compute_file_hash(file_path)
        doc_type = "markdown" if file_path.suffix.lower() in {".md", ".markdown"} else "text"
        document_metadata = self.build_metadata(
            file_path,
            doc_type=doc_type,
            metadata=combined_metadata,
            file_hash=file_hash,
        )
        document_metadata.setdefault("images", [])
        return Document(
            id=self.build_document_id(file_hash),
            text=text,
            metadata=document_metadata,
        )

    @staticmethod
    def _parse_front_matter(text: str, path: Path) -> tuple[dict[str, Any], str]:
        if path.suffix.lower() not in {".md", ".markdown"} or not text.startswith("---\n"):
            return {}, text

        closing_marker = text.find("\n---\n", 4)
        if closing_marker < 0:
            raise ValueError(f"Unclosed YAML front matter in {path}")
        raw_metadata = text[4:closing_marker]
        parsed = yaml.safe_load(raw_metadata) or {}
        if not isinstance(parsed, dict):
            raise ValueError(f"YAML front matter must be a mapping in {path}")
        return parsed, text[closing_marker + 5 :]
