"""Typed application settings loaded from YAML."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SettingsError(ValueError):
    """Raised when application configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    name: str
    environment: str


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    provider: str
    model: str | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    provider: str
    model: str
    dimensions: int
    batch_size: int
    cache_dir: Path


@dataclass(frozen=True, slots=True)
class SplitterSettings:
    provider: str
    chunk_size: int
    chunk_overlap: int


@dataclass(frozen=True, slots=True)
class TransformSettings:
    enabled: bool
    use_llm: bool
    prompt_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ImageCaptionerSettings:
    enabled: bool
    prompt_path: Path
    append_to_text: bool


@dataclass(frozen=True, slots=True)
class ImageStorageSettings:
    enabled: bool
    root_path: Path
    database_path: Path
    collection: str


@dataclass(frozen=True, slots=True)
class IngestionSettings:
    extract_images: bool
    chunk_refiner: TransformSettings
    metadata_enricher: TransformSettings
    image_captioner: ImageCaptionerSettings
    image_storage: ImageStorageSettings


@dataclass(frozen=True, slots=True)
class VectorStoreSettings:
    backend: str
    persist_path: Path
    collection_name: str


@dataclass(frozen=True, slots=True)
class RetrievalSettings:
    sparse_backend: str
    fusion_algorithm: str
    top_k_dense: int
    top_k_sparse: int
    top_k_final: int
    rrf_k: int
    max_chunks_per_document: int
    min_fused_score: float


@dataclass(frozen=True, slots=True)
class RerankSettings:
    backend: str
    model: str | None
    top_m: int


@dataclass(frozen=True, slots=True)
class EvaluationSettings:
    backends: tuple[str, ...]
    golden_test_set: Path


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    enabled: bool
    trace_file: Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings used to construct application components."""

    project: ProjectSettings
    llm: ProviderSettings
    vision_llm: ProviderSettings
    embedding: EmbeddingSettings
    splitter: SplitterSettings
    ingestion: IngestionSettings
    vector_store: VectorStoreSettings
    retrieval: RetrievalSettings
    rerank: RerankSettings
    evaluation: EvaluationSettings
    observability: ObservabilitySettings


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    """Read a YAML file, expand environment references, and validate its content."""
    settings_path = Path(path)
    if not settings_path.is_file():
        raise SettingsError(f"Settings file does not exist: {settings_path}")

    try:
        raw = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SettingsError(f"Invalid YAML in {settings_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SettingsError("Settings root must be a mapping")

    expanded = _expand_environment(raw)
    settings = _build_settings(expanded)
    validate_settings(settings)
    return settings


def validate_settings(settings: Settings) -> None:
    """Validate cross-field and value constraints with field-specific messages."""
    required_text = {
        "project.name": settings.project.name,
        "project.environment": settings.project.environment,
        "llm.provider": settings.llm.provider,
        "vision_llm.provider": settings.vision_llm.provider,
        "embedding.provider": settings.embedding.provider,
        "embedding.model": settings.embedding.model,
        "splitter.provider": settings.splitter.provider,
        "ingestion.image_storage.collection": settings.ingestion.image_storage.collection,
        "vector_store.backend": settings.vector_store.backend,
        "retrieval.sparse_backend": settings.retrieval.sparse_backend,
        "retrieval.fusion_algorithm": settings.retrieval.fusion_algorithm,
        "rerank.backend": settings.rerank.backend,
    }
    for field_path, value in required_text.items():
        if not value.strip():
            raise SettingsError(f"Missing required setting: {field_path}")

    positive_values = {
        "embedding.batch_size": settings.embedding.batch_size,
        "embedding.dimensions": settings.embedding.dimensions,
        "splitter.chunk_size": settings.splitter.chunk_size,
        "retrieval.top_k_dense": settings.retrieval.top_k_dense,
        "retrieval.top_k_sparse": settings.retrieval.top_k_sparse,
        "retrieval.top_k_final": settings.retrieval.top_k_final,
        "retrieval.rrf_k": settings.retrieval.rrf_k,
        "retrieval.max_chunks_per_document": (settings.retrieval.max_chunks_per_document),
        "rerank.top_m": settings.rerank.top_m,
    }
    for field_path, value in positive_values.items():
        if value <= 0:
            raise SettingsError(f"Setting {field_path} must be greater than 0")

    if settings.splitter.chunk_overlap < 0:
        raise SettingsError("Setting splitter.chunk_overlap must be greater than or equal to 0")
    if settings.splitter.chunk_overlap >= settings.splitter.chunk_size:
        raise SettingsError(
            "Setting splitter.chunk_overlap must be smaller than splitter.chunk_size"
        )

    if settings.retrieval.top_k_final > (
        settings.retrieval.top_k_dense + settings.retrieval.top_k_sparse
    ):
        raise SettingsError(
            "Setting retrieval.top_k_final cannot exceed the total retrieval candidate count"
        )
    if settings.retrieval.min_fused_score < 0:
        raise SettingsError("Setting retrieval.min_fused_score must be non-negative")
    if not settings.evaluation.backends:
        raise SettingsError("Missing required setting: evaluation.backends")


def _build_settings(raw: dict[str, Any]) -> Settings:
    project = _section(raw, "project")
    llm = _section(raw, "llm")
    vision_llm = _optional_section(raw, "vision_llm")
    embedding = _section(raw, "embedding")
    splitter = _section(raw, "splitter")
    ingestion = _optional_section(raw, "ingestion")
    chunk_refiner = _optional_section(ingestion, "chunk_refiner")
    metadata_enricher = _optional_section(ingestion, "metadata_enricher")
    image_captioner = _optional_section(ingestion, "image_captioner")
    image_storage = _optional_section(ingestion, "image_storage")
    vector_store = _section(raw, "vector_store")
    retrieval = _section(raw, "retrieval")
    rerank = _section(raw, "rerank")
    evaluation = _section(raw, "evaluation")
    observability = _section(raw, "observability")

    backends = _required(evaluation, "backends", "evaluation.backends")
    if not isinstance(backends, list) or not all(isinstance(item, str) for item in backends):
        raise SettingsError("Setting evaluation.backends must be a list of strings")

    return Settings(
        project=ProjectSettings(
            name=_required_str(project, "name", "project.name"),
            environment=_required_str(project, "environment", "project.environment"),
        ),
        llm=ProviderSettings(
            provider=_required_str(llm, "provider", "llm.provider"),
            model=_optional_str(llm.get("model"), "llm.model"),
        ),
        vision_llm=ProviderSettings(
            provider=_optional_non_empty_str(
                vision_llm.get("provider"),
                "vision_llm.provider",
                default="disabled",
            ),
            model=_optional_str(vision_llm.get("model"), "vision_llm.model"),
        ),
        embedding=EmbeddingSettings(
            provider=_required_str(embedding, "provider", "embedding.provider"),
            model=_required_str(embedding, "model", "embedding.model"),
            dimensions=_required_int(embedding, "dimensions", "embedding.dimensions"),
            batch_size=_required_int(embedding, "batch_size", "embedding.batch_size"),
            cache_dir=Path(_required_str(embedding, "cache_dir", "embedding.cache_dir")),
        ),
        splitter=SplitterSettings(
            provider=_required_str(splitter, "provider", "splitter.provider"),
            chunk_size=_required_int(splitter, "chunk_size", "splitter.chunk_size"),
            chunk_overlap=_required_int(splitter, "chunk_overlap", "splitter.chunk_overlap"),
        ),
        ingestion=IngestionSettings(
            extract_images=_optional_bool(
                ingestion.get("extract_images"),
                "ingestion.extract_images",
                default=False,
            ),
            chunk_refiner=TransformSettings(
                enabled=_optional_bool(
                    chunk_refiner.get("enabled"),
                    "ingestion.chunk_refiner.enabled",
                    default=True,
                ),
                use_llm=_optional_bool(
                    chunk_refiner.get("use_llm"),
                    "ingestion.chunk_refiner.use_llm",
                    default=False,
                ),
                prompt_path=_optional_path(
                    chunk_refiner.get("prompt_path"),
                    "ingestion.chunk_refiner.prompt_path",
                ),
            ),
            metadata_enricher=TransformSettings(
                enabled=_optional_bool(
                    metadata_enricher.get("enabled"),
                    "ingestion.metadata_enricher.enabled",
                    default=True,
                ),
                use_llm=_optional_bool(
                    metadata_enricher.get("use_llm"),
                    "ingestion.metadata_enricher.use_llm",
                    default=False,
                ),
                prompt_path=_optional_path(
                    metadata_enricher.get("prompt_path"),
                    "ingestion.metadata_enricher.prompt_path",
                ),
            ),
            image_captioner=ImageCaptionerSettings(
                enabled=_optional_bool(
                    image_captioner.get("enabled"),
                    "ingestion.image_captioner.enabled",
                    default=False,
                ),
                prompt_path=Path(
                    _optional_non_empty_str(
                        image_captioner.get("prompt_path"),
                        "ingestion.image_captioner.prompt_path",
                        default="config/prompts/image_captioning.txt",
                    )
                ),
                append_to_text=_optional_bool(
                    image_captioner.get("append_to_text"),
                    "ingestion.image_captioner.append_to_text",
                    default=True,
                ),
            ),
            image_storage=ImageStorageSettings(
                enabled=_optional_bool(
                    image_storage.get("enabled"),
                    "ingestion.image_storage.enabled",
                    default=True,
                ),
                root_path=Path(
                    _optional_non_empty_str(
                        image_storage.get("root_path"),
                        "ingestion.image_storage.root_path",
                        default="data/images",
                    )
                ),
                database_path=Path(
                    _optional_non_empty_str(
                        image_storage.get("database_path"),
                        "ingestion.image_storage.database_path",
                        default="data/db/image_index.db",
                    )
                ),
                collection=_optional_non_empty_str(
                    image_storage.get("collection"),
                    "ingestion.image_storage.collection",
                    default="wms-system-training",
                ),
            ),
        ),
        vector_store=VectorStoreSettings(
            backend=_required_str(vector_store, "backend", "vector_store.backend"),
            persist_path=Path(
                _required_str(vector_store, "persist_path", "vector_store.persist_path")
            ),
            collection_name=_required_str(
                vector_store, "collection_name", "vector_store.collection_name"
            ),
        ),
        retrieval=RetrievalSettings(
            sparse_backend=_required_str(retrieval, "sparse_backend", "retrieval.sparse_backend"),
            fusion_algorithm=_required_str(
                retrieval, "fusion_algorithm", "retrieval.fusion_algorithm"
            ),
            top_k_dense=_required_int(retrieval, "top_k_dense", "retrieval.top_k_dense"),
            top_k_sparse=_required_int(retrieval, "top_k_sparse", "retrieval.top_k_sparse"),
            top_k_final=_required_int(retrieval, "top_k_final", "retrieval.top_k_final"),
            rrf_k=_required_int(retrieval, "rrf_k", "retrieval.rrf_k"),
            max_chunks_per_document=_required_int(
                retrieval,
                "max_chunks_per_document",
                "retrieval.max_chunks_per_document",
            ),
            min_fused_score=_required_number(
                retrieval, "min_fused_score", "retrieval.min_fused_score"
            ),
        ),
        rerank=RerankSettings(
            backend=_required_str(rerank, "backend", "rerank.backend"),
            model=_optional_str(rerank.get("model"), "rerank.model"),
            top_m=_required_int(rerank, "top_m", "rerank.top_m"),
        ),
        evaluation=EvaluationSettings(
            backends=tuple(backends),
            golden_test_set=Path(
                _required_str(evaluation, "golden_test_set", "evaluation.golden_test_set")
            ),
        ),
        observability=ObservabilitySettings(
            enabled=_required_bool(observability, "enabled", "observability.enabled"),
            trace_file=Path(_required_str(observability, "trace_file", "observability.trace_file")),
        ),
    )


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise SettingsError(f"Missing required setting section: {key}")
    return value


def _optional_section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise SettingsError(f"Setting section {key} must be a mapping")
    return value


def _required(section: dict[str, Any], key: str, field_path: str) -> Any:
    if key not in section or section[key] is None:
        raise SettingsError(f"Missing required setting: {field_path}")
    return section[key]


def _required_str(section: dict[str, Any], key: str, field_path: str) -> str:
    value = _required(section, key, field_path)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"Setting {field_path} must be a non-empty string")
    return value


def _required_int(section: dict[str, Any], key: str, field_path: str) -> int:
    value = _required(section, key, field_path)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError(f"Setting {field_path} must be an integer")
    return value


def _required_bool(section: dict[str, Any], key: str, field_path: str) -> bool:
    value = _required(section, key, field_path)
    if not isinstance(value, bool):
        raise SettingsError(f"Setting {field_path} must be a boolean")
    return value


def _required_number(section: dict[str, Any], key: str, field_path: str) -> float:
    value = _required(section, key, field_path)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsError(f"Setting {field_path} must be a number")
    return float(value)


def _optional_str(value: Any, field_path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(f"Setting {field_path} must be a string or null")
    return value


def _optional_non_empty_str(value: Any, field_path: str, *, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"Setting {field_path} must be a non-empty string")
    return value


def _optional_bool(value: Any, field_path: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SettingsError(f"Setting {field_path} must be a boolean")
    return value


def _optional_path(value: Any, field_path: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"Setting {field_path} must be a non-empty string or null")
    return Path(value)


def _expand_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        if variable not in os.environ:
            raise SettingsError(f"Environment variable is not set: {variable}")
        return os.environ[variable]

    return _ENV_PATTERN.sub(replace, value)
