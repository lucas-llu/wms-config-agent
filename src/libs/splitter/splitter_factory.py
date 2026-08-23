"""Configuration-driven splitter factory."""

from __future__ import annotations

from collections.abc import Callable

from core.settings import Settings, SplitterSettings
from libs.splitter.base_splitter import BaseSplitter
from libs.splitter.recursive_splitter import RecursiveSplitter

SplitterBuilder = Callable[[int, int], BaseSplitter]


class SplitterFactory:
    """Create registered splitter providers from validated settings."""

    _providers: dict[str, SplitterBuilder] = {
        "recursive": lambda chunk_size, chunk_overlap: RecursiveSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    }

    @classmethod
    def create(cls, settings: Settings | SplitterSettings) -> BaseSplitter:
        splitter_settings = settings.splitter if isinstance(settings, Settings) else settings
        try:
            builder = cls._providers[splitter_settings.provider]
        except KeyError as exc:
            supported = ", ".join(sorted(cls._providers))
            raise ValueError(
                f"Unknown splitter provider '{splitter_settings.provider}'; "
                f"supported providers: {supported}"
            ) from exc
        return builder(splitter_settings.chunk_size, splitter_settings.chunk_overlap)

    @classmethod
    def register(cls, provider: str, builder: SplitterBuilder, *, replace: bool = False) -> None:
        if not provider.strip():
            raise ValueError("provider must be a non-empty string")
        if provider in cls._providers and not replace:
            raise ValueError(f"Splitter provider is already registered: {provider}")
        cls._providers[provider] = builder
