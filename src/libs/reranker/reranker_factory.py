"""Configuration-driven reranker factory."""

from __future__ import annotations

from collections.abc import Callable

from core.settings import RerankSettings, Settings
from libs.reranker.base_reranker import BaseReranker
from libs.reranker.none_reranker import NoneReranker

RerankerBuilder = Callable[[RerankSettings], BaseReranker]


class RerankerFactory:
    _providers: dict[str, RerankerBuilder] = {"none": lambda settings: NoneReranker()}

    @classmethod
    def create(cls, settings: Settings | RerankSettings) -> BaseReranker:
        rerank_settings = settings.rerank if isinstance(settings, Settings) else settings
        try:
            builder = cls._providers[rerank_settings.backend]
        except KeyError as exc:
            supported = ", ".join(sorted(cls._providers))
            raise ValueError(
                f"Unknown reranker backend '{rerank_settings.backend}'; "
                f"supported backends: {supported}"
            ) from exc
        return builder(rerank_settings)

    @classmethod
    def register(
        cls,
        backend: str,
        builder: RerankerBuilder,
        *,
        replace: bool = False,
    ) -> None:
        if not backend.strip():
            raise ValueError("backend must be a non-empty string")
        if backend in cls._providers and not replace:
            raise ValueError(f"Reranker backend is already registered: {backend}")
        cls._providers[backend] = builder
