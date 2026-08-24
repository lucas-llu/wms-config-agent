"""Privacy-safe configuration summaries for the Dashboard."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.settings import Settings, load_settings


@dataclass(frozen=True, slots=True)
class ComponentConfig:
    component: str
    provider: str
    details: str
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConfigService:
    def __init__(self, settings: Settings, *, settings_path: str | Path) -> None:
        self.settings = settings
        self.settings_path = Path(settings_path)

    @classmethod
    def from_path(cls, path: str | Path = "config/settings.yaml") -> ConfigService:
        return cls(load_settings(path), settings_path=path)

    def components(self) -> list[ComponentConfig]:
        settings = self.settings
        return [
            ComponentConfig(
                "LLM",
                settings.llm.provider,
                settings.llm.model or "No model configured",
                settings.llm.provider.lower() != "disabled",
            ),
            ComponentConfig(
                "Vision LLM",
                settings.vision_llm.provider,
                settings.vision_llm.model or "No model configured",
                settings.vision_llm.provider.lower() != "disabled",
            ),
            ComponentConfig(
                "Embedding",
                settings.embedding.provider,
                f"{settings.embedding.model} · {settings.embedding.dimensions} dimensions",
            ),
            ComponentConfig(
                "Splitter",
                settings.splitter.provider,
                f"size {settings.splitter.chunk_size} · overlap {settings.splitter.chunk_overlap}",
            ),
            ComponentConfig(
                "Vector store",
                settings.vector_store.backend,
                settings.vector_store.collection_name,
            ),
            ComponentConfig(
                "Sparse retrieval",
                settings.retrieval.sparse_backend,
                f"top {settings.retrieval.top_k_sparse}",
            ),
            ComponentConfig(
                "Reranker",
                settings.rerank.backend,
                settings.rerank.model or "Pass-through",
                settings.rerank.backend.lower() != "none",
            ),
            ComponentConfig(
                "Tracing",
                "local JSONL",
                str(settings.observability.trace_file),
                settings.observability.enabled,
            ),
        ]

    def project_summary(self) -> dict[str, str]:
        return {
            "name": self.settings.project.name,
            "environment": self.settings.project.environment,
            "settings_path": str(self.settings_path),
        }
