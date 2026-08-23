"""Configuration-driven vector store factory."""

from __future__ import annotations

from collections.abc import Callable

from core.settings import Settings, VectorStoreSettings
from libs.vector_store.base_vector_store import BaseVectorStore
from libs.vector_store.chroma_store import ChromaStore

VectorStoreBuilder = Callable[[VectorStoreSettings], BaseVectorStore]


class VectorStoreFactory:
    _providers: dict[str, VectorStoreBuilder] = {
        "chroma": lambda settings: ChromaStore(
            persist_path=settings.persist_path,
            collection_name=settings.collection_name,
        )
    }

    @classmethod
    def create(cls, settings: Settings | VectorStoreSettings) -> BaseVectorStore:
        vector_settings = settings.vector_store if isinstance(settings, Settings) else settings
        try:
            builder = cls._providers[vector_settings.backend]
        except KeyError as exc:
            supported = ", ".join(sorted(cls._providers))
            raise ValueError(
                f"Unknown vector store backend '{vector_settings.backend}'; "
                f"supported backends: {supported}"
            ) from exc
        return builder(vector_settings)
