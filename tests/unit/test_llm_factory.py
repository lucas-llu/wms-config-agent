from __future__ import annotations

from pathlib import Path

import pytest

from core.settings import ProviderSettings
from libs.llm import (
    BaseLLM,
    BaseVisionLLM,
    ChatResponse,
    DisabledLLM,
    DisabledVisionLLM,
    LLMFactory,
)


class FakeTextLLM(BaseLLM):
    def chat(self, messages, trace=None) -> ChatResponse:
        return ChatResponse(messages[-1]["content"], model="fake-text")


class FakeVisionLLM(BaseVisionLLM):
    def chat_with_image(self, text, image_path, trace=None) -> ChatResponse:
        return ChatResponse(f"{text}:{Path(image_path).name}", model="fake-vision")


def test_disabled_factories_return_explicit_fallback_providers() -> None:
    settings = ProviderSettings(provider="disabled")

    assert isinstance(LLMFactory.create(settings), DisabledLLM)
    assert isinstance(LLMFactory.create_vision_llm(settings), DisabledVisionLLM)


def test_registered_text_provider_uses_base_llm_contract() -> None:
    LLMFactory.register_text("unit-fake-text", lambda settings: FakeTextLLM())

    llm = LLMFactory.create(ProviderSettings(provider="unit-fake-text"))

    assert llm.chat([{"role": "user", "content": "hello"}]).content == "hello"


def test_registered_vision_provider_uses_documented_contract(tmp_path: Path) -> None:
    LLMFactory.register_vision("unit-fake-vision", lambda settings: FakeVisionLLM())
    image = tmp_path / "diagram.png"
    image.write_bytes(b"image")

    vision = LLMFactory.create_vision_llm(
        ProviderSettings(provider="unit-fake-vision")
    )

    assert vision.chat_with_image("describe", image).content == "describe:diagram.png"


def test_factory_rejects_unknown_and_duplicate_providers() -> None:
    with pytest.raises(ValueError, match="Unknown text LLM provider"):
        LLMFactory.create(ProviderSettings(provider="missing-provider"))
    with pytest.raises(ValueError, match="already registered"):
        LLMFactory.register_text("disabled", lambda settings: FakeTextLLM())
