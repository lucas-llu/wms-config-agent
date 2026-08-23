"""Optional image caption enrichment with non-blocking fallback behavior."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.settings import ImageCaptionerSettings, Settings
from core.types import Chunk
from ingestion.transform.base_transform import BaseTransform
from libs.llm import BaseVisionLLM

_DEFAULT_PROMPT = """Describe this WMS/JDA MOCA document image using only visible evidence.
Preserve UI labels, configuration keys, commands, table values, and warnings.

Nearby document context:
{context}
"""
_CAPTION_MARKER = "[IMAGE DESCRIPTIONS]"


class ImageCaptioner(BaseTransform):
    """Generate captions when a Vision LLM is available and degrade otherwise."""

    name = "image_captioner"

    def __init__(
        self,
        settings: Settings | ImageCaptionerSettings,
        vision_llm: BaseVisionLLM | None = None,
        prompt_path: str | Path | None = None,
    ) -> None:
        config = settings.ingestion.image_captioner if isinstance(settings, Settings) else settings
        self.enabled = config.enabled
        self.append_to_text = config.append_to_text
        self.vision_llm = vision_llm
        self.prompt = self._load_prompt(prompt_path or config.prompt_path)

    def transform(self, chunks: list[Chunk], trace: Any | None = None) -> list[Chunk]:
        started = time.perf_counter()
        result: list[Chunk] = []
        captioned_count = 0
        unprocessed_count = 0
        for chunk in chunks:
            metadata = dict(chunk.metadata)
            image_refs = self._image_refs(metadata)
            if not image_refs:
                result.append(self.clone_chunk(chunk, metadata=metadata))
                continue

            if not self.enabled or self.vision_llm is None:
                metadata["has_unprocessed_images"] = True
                metadata["image_caption_status"] = (
                    "disabled" if not self.enabled else "vision_llm_unavailable"
                )
                unprocessed_count += len(image_refs)
                result.append(self.clone_chunk(chunk, metadata=metadata))
                continue

            captions, failures, images = self._caption_images(chunk, metadata)
            metadata["images"] = images
            metadata["image_captions"] = captions
            if failures:
                metadata["has_unprocessed_images"] = True
                metadata["image_caption_status"] = "partial" if captions else "failed"
                metadata["image_caption_failures"] = failures
                unprocessed_count += len(failures)
            else:
                metadata.pop("has_unprocessed_images", None)
                metadata.pop("image_caption_failures", None)
                metadata["image_caption_status"] = "complete"
            captioned_count += len(captions)

            text = chunk.text
            if self.append_to_text and captions and _CAPTION_MARKER not in text:
                lines = [f"- [{item['image_id']}] {item['caption']}" for item in captions]
                text = f"{text.rstrip()}\n\n{_CAPTION_MARKER}\n" + "\n".join(lines)
                metadata["image_caption_text_appended"] = True
            result.append(self.clone_chunk(chunk, text=text, metadata=metadata))

        self.record_trace(
            trace,
            name=self.name,
            started=started,
            details={
                "input_count": len(chunks),
                "captioned_count": captioned_count,
                "unprocessed_count": unprocessed_count,
                "disabled": not self.enabled,
            },
        )
        return result

    def _caption_images(
        self, chunk: Chunk, metadata: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
        existing = {
            str(item.get("image_id")): item
            for item in metadata.get("image_captions", [])
            if isinstance(item, dict) and item.get("image_id") and item.get("caption")
        }
        captions: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        images: list[dict[str, Any]] = []
        handled_refs: set[str] = set()
        for original in metadata.get("images", []):
            if not isinstance(original, dict):
                continue
            image = dict(original)
            image_id = str(image.get("id") or "").strip()
            if not image_id:
                continue
            handled_refs.add(image_id)
            if image_id in existing:
                item = dict(existing[image_id])
                captions.append(item)
                image["caption"] = item["caption"]
                images.append(image)
                continue
            path_value = image.get("path")
            if not isinstance(path_value, str) or not Path(path_value).is_file():
                failures.append({"image_id": image_id, "reason": "image_missing"})
                images.append(image)
                continue
            try:
                caption = self._generate_caption(
                    image_path=Path(path_value), context=chunk.text[:2000]
                )
                if not caption:
                    raise ValueError("empty caption")
                item = {
                    "image_id": image_id,
                    "caption": caption,
                    "page": image.get("page"),
                }
                captions.append(item)
                image["caption"] = caption
            except Exception as exc:
                failures.append({"image_id": image_id, "reason": type(exc).__name__})
            images.append(image)
        for image_id in self._image_refs(metadata):
            if image_id in handled_refs:
                continue
            if image_id in existing:
                captions.append(dict(existing[image_id]))
            else:
                failures.append({"image_id": image_id, "reason": "image_metadata_missing"})
        return captions, failures, images

    def _generate_caption(self, *, image_path: Path, context: str) -> str:
        prompt = self.prompt.format(context=context)
        response = self.vision_llm.chat_with_image(prompt, image_path)
        return response.content.strip()

    @staticmethod
    def _image_refs(metadata: dict[str, Any]) -> list[str]:
        refs = metadata.get("image_refs", [])
        if isinstance(refs, list):
            return [str(ref) for ref in refs if str(ref).strip()]
        return []

    @staticmethod
    def _load_prompt(prompt_path: str | Path | None) -> str:
        if prompt_path is None:
            return _DEFAULT_PROMPT
        try:
            prompt = Path(prompt_path).read_text(encoding="utf-8")
        except OSError:
            return _DEFAULT_PROMPT
        return prompt if "{context}" in prompt else f"{prompt.rstrip()}\n\n{{context}}\n"
