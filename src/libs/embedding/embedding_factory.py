"""Configuration-driven embedding factory."""

from __future__ import annotations

from collections.abc import Callable

from core.settings import EmbeddingSettings, Settings
from libs.embedding.base_embedding import BaseEmbedding
from libs.embedding.local_lsa_embedding import LocalLSAEmbedding

EmbeddingBuilder = Callable[[EmbeddingSettings], BaseEmbedding]


class EmbeddingFactory:
    _providers: dict[str, EmbeddingBuilder] = {
        "local_lsa": lambda settings: LocalLSAEmbedding(
            model_name=settings.model,
            dimensions=settings.dimensions,
            cache_dir=settings.cache_dir,
            batch_size=settings.batch_size,
        )
    }

    @classmethod
    def create(cls, settings: Settings | EmbeddingSettings) -> BaseEmbedding:
        embedding_settings = settings.embedding if isinstance(settings, Settings) else settings
        try:
            builder = cls._providers[embedding_settings.provider]
        except KeyError as exc:
            supported = ", ".join(sorted(cls._providers))
            raise ValueError(
                f"Unknown embedding provider '{embedding_settings.provider}'; "
                f"supported providers: {supported}"
            ) from exc
        return builder(embedding_settings)

    @classmethod
    def register(
        cls,
        provider: str,
        builder: EmbeddingBuilder,
        *,
        replace: bool = False,
    ) -> None:
        if not provider.strip():
            raise ValueError("provider must be a non-empty string")
        if provider in cls._providers and not replace:
            raise ValueError(f"Embedding provider is already registered: {provider}")
        cls._providers[provider] = builder
