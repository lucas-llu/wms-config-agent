from __future__ import annotations

from pathlib import Path

from core.settings import ImageCaptionerSettings
from core.types import Chunk
from ingestion.transform import ImageCaptioner
from libs.llm import ChatResponse


def _settings(tmp_path: Path, *, enabled: bool) -> ImageCaptionerSettings:
    return ImageCaptionerSettings(
        enabled=enabled,
        prompt_path=tmp_path / "missing-prompt.txt",
        append_to_text=True,
    )


def _chunk(image: Path) -> Chunk:
    text = "Configuration diagram\n[IMAGE: img-1]"
    return Chunk(
        id="chunk-1",
        text=text,
        metadata={
            "source_path": "manual.pdf",
            "image_refs": ["img-1"],
            "images": [{"id": "img-1", "path": str(image), "page": 2}],
        },
        start_offset=0,
        end_offset=len(text),
        source_ref="doc-1",
    )


def test_disabled_captioner_preserves_refs_and_marks_unprocessed(tmp_path: Path) -> None:
    image = tmp_path / "diagram.png"
    image.write_bytes(b"image")

    output = ImageCaptioner(_settings(tmp_path, enabled=False)).transform([_chunk(image)])[0]

    assert output.metadata["image_refs"] == ["img-1"]
    assert output.metadata["has_unprocessed_images"] is True
    assert output.metadata["image_caption_status"] == "disabled"
    assert "image_captions" not in output.metadata


def test_missing_vision_llm_degrades_without_blocking(tmp_path: Path) -> None:
    image = tmp_path / "diagram.png"
    image.write_bytes(b"image")

    output = ImageCaptioner(_settings(tmp_path, enabled=True)).transform([_chunk(image)])[0]

    assert output.metadata["image_caption_status"] == "vision_llm_unavailable"
    assert output.text.startswith("Configuration diagram")


def test_mock_vision_caption_is_persisted_and_appended_idempotently(tmp_path: Path) -> None:
    image = tmp_path / "diagram.png"
    image.write_bytes(b"image")

    class FakeVision:
        calls = 0

        def chat_with_image(self, text: str, image_path: Path, trace=None) -> ChatResponse:
            self.calls += 1
            assert "Configuration diagram" in text
            assert image_path == image
            return ChatResponse("The image shows the MOCA policy screen.")

    vision = FakeVision()
    captioner = ImageCaptioner(_settings(tmp_path, enabled=True), vision_llm=vision)

    first = captioner.transform([_chunk(image)])[0]
    second = captioner.transform([first])[0]

    assert first.metadata["image_caption_status"] == "complete"
    assert first.metadata["image_captions"][0]["image_id"] == "img-1"
    assert first.metadata["images"][0]["caption"].startswith("The image")
    assert first.text.count("[IMAGE DESCRIPTIONS]") == 1
    assert second.text == first.text
    assert vision.calls == 1


def test_vision_error_marks_failed_and_keeps_original_text(tmp_path: Path) -> None:
    image = tmp_path / "diagram.png"
    image.write_bytes(b"image")

    class BrokenVision:
        @staticmethod
        def chat_with_image(text: str, image_path: Path, trace=None) -> ChatResponse:
            raise RuntimeError("provider offline")

    original = _chunk(image)
    output = ImageCaptioner(_settings(tmp_path, enabled=True), vision_llm=BrokenVision()).transform(
        [original]
    )[0]

    assert output.text == original.text
    assert output.metadata["image_caption_status"] == "failed"
    assert output.metadata["image_caption_failures"] == [
        {"image_id": "img-1", "reason": "RuntimeError"}
    ]
