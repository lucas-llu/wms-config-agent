"""Safely attach locally extracted images to MCP content blocks."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from core.types import RetrievalResult


class MultimodalAssembler:
    def __init__(
        self,
        allowed_roots: list[str | Path],
        *,
        max_images: int = 3,
        max_image_bytes: int = 5_000_000,
    ) -> None:
        self.allowed_roots = tuple(Path(root).resolve() for root in allowed_roots)
        self.max_images = max_images
        self.max_image_bytes = max_image_bytes

    def assemble(self, results: list[RetrievalResult]) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for result in results:
            for image in result.metadata.get("images", []):
                if len(content) >= self.max_images:
                    return content
                path_value = image.get("path") if isinstance(image, dict) else None
                if not isinstance(path_value, str):
                    continue
                path = Path(path_value).resolve()
                if path in seen or not self._is_allowed(path) or not path.is_file():
                    continue
                if path.stat().st_size > self.max_image_bytes:
                    continue
                mime_type, _ = mimetypes.guess_type(path.name)
                if not mime_type or not mime_type.startswith("image/"):
                    continue
                seen.add(path)
                content.append(
                    {
                        "type": "image",
                        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                        "mimeType": mime_type,
                    }
                )
        return content

    def _is_allowed(self, path: Path) -> bool:
        return any(path.is_relative_to(root) for root in self.allowed_roots)
