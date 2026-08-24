"""Registry-backed construction for text and Vision LLM providers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.settings import ProviderSettings, Settings
from libs.llm.base_llm import BaseLLM, ChatMessage, ChatResponse
from libs.llm.base_vision_llm import BaseVisionLLM
from libs.llm.openai_compatible_llm import OpenAICompatibleLLM

TextLLMBuilder = Callable[[ProviderSettings], BaseLLM]
VisionLLMBuilder = Callable[[ProviderSettings], BaseVisionLLM]


class DisabledLLM(BaseLLM):
    """Explicit local fallback used when no text provider is configured."""

    def chat(
        self,
        messages: list[ChatMessage],
        trace: Any | None = None,
    ) -> ChatResponse:
        del messages, trace
        raise RuntimeError("Text LLM provider is disabled")


class DisabledVisionLLM(BaseVisionLLM):
    """Explicit local fallback used when no Vision provider is configured."""

    def chat_with_image(
        self,
        text: str,
        image_path: str | Path | bytes,
        trace: Any | None = None,
    ) -> ChatResponse:
        del text, image_path, trace
        raise RuntimeError("Vision LLM provider is disabled")


class LLMFactory:
    """Create configured providers without coupling transforms to vendor SDKs."""

    _text_providers: dict[str, TextLLMBuilder] = {
        "disabled": lambda settings: DisabledLLM(),
        "openai_compatible": OpenAICompatibleLLM,
    }
    _vision_providers: dict[str, VisionLLMBuilder] = {
        "disabled": lambda settings: DisabledVisionLLM(),
    }

    @classmethod
    def register_text(
        cls,
        provider: str,
        builder: TextLLMBuilder,
        *,
        replace: bool = False,
    ) -> None:
        cls._register(cls._text_providers, provider, builder, replace=replace)

    @classmethod
    def register_vision(
        cls,
        provider: str,
        builder: VisionLLMBuilder,
        *,
        replace: bool = False,
    ) -> None:
        cls._register(cls._vision_providers, provider, builder, replace=replace)

    @classmethod
    def create(cls, settings: Settings | ProviderSettings) -> BaseLLM:
        provider_settings = settings.llm if isinstance(settings, Settings) else settings
        return cls._create(cls._text_providers, provider_settings, "text")

    @classmethod
    def create_vision_llm(cls, settings: Settings | ProviderSettings) -> BaseVisionLLM:
        provider_settings = settings.vision_llm if isinstance(settings, Settings) else settings
        return cls._create(cls._vision_providers, provider_settings, "vision")

    @staticmethod
    def _register(
        providers: dict[str, Any],
        provider: str,
        builder: Any,
        *,
        replace: bool,
    ) -> None:
        name = provider.strip().lower()
        if not name:
            raise ValueError("provider name must not be empty")
        if name in providers and not replace:
            raise ValueError(f"Provider '{name}' is already registered")
        providers[name] = builder

    @staticmethod
    def _create(
        providers: dict[str, Any],
        settings: ProviderSettings,
        provider_type: str,
    ) -> Any:
        name = settings.provider.strip().lower()
        try:
            builder = providers[name]
        except KeyError as exc:
            supported = ", ".join(sorted(providers))
            raise ValueError(
                f"Unknown {provider_type} LLM provider '{name}'; supported providers: {supported}"
            ) from exc
        return builder(settings)
