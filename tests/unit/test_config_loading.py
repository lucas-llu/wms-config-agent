from pathlib import Path

import pytest

from core.settings import SettingsError, load_settings


def test_load_project_settings() -> None:
    settings = load_settings("config/settings.yaml")

    assert settings.project.name == "wms-config-agent"
    assert settings.llm.provider == "openai_compatible"
    assert settings.llm.model == "ox-alpha-free"
    assert settings.llm.base_url == "https://opencode.ai/zen/go/v1/chat/completions"
    assert settings.llm.api_key_env == "WMS_LLM_API_KEY"
    assert settings.llm.timeout_seconds == 60
    assert settings.llm.max_tokens == 1024
    assert settings.llm.temperature == 0
    assert settings.llm.max_retries == 2
    assert settings.llm.retry_backoff_seconds == 0.5
    assert settings.vision_llm.provider == "disabled"
    assert settings.embedding.provider == "local_lsa"
    assert settings.embedding.dimensions == 256
    assert settings.embedding.batch_size == 32
    assert settings.splitter.chunk_size == 1000
    assert settings.vector_store.backend == "chroma"
    assert settings.vector_store.collection_name == "wms_config_chunks"
    assert settings.retrieval.top_k_final == 5
    assert settings.retrieval.rrf_k == 60
    assert settings.retrieval.max_chunks_per_document == 2


def test_missing_required_field_has_readable_path(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        """
project: {name: test, environment: test}
llm: {provider: disabled}
embedding:
  {model: test-model, dimensions: 4, batch_size: 2, cache_dir: data/models}
splitter: {provider: recursive, chunk_size: 1000, chunk_overlap: 100}
vector_store: {backend: memory, persist_path: data/db, collection_name: test}
retrieval:
  {sparse_backend: bm25, fusion_algorithm: rrf, top_k_dense: 2, top_k_sparse: 2,
   top_k_final: 2, rrf_k: 60, max_chunks_per_document: 2, min_fused_score: 0.02}
rerank: {backend: none, model: null, top_m: 2}
evaluation: {backends: [custom], golden_test_set: tests/fixtures/golden_test_set.json}
observability: {enabled: true, trace_file: logs/traces.jsonl}
""",
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match=r"embedding\.provider"):
        load_settings(config_path)


def test_environment_reference_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path("config/settings.yaml").read_text(encoding="utf-8")
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        original.replace("provider: openai_compatible", "provider: ${TEST_PROVIDER}", 1),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_PROVIDER", "offline")

    assert load_settings(config_path).llm.provider == "offline"


def test_openai_compatible_provider_requires_model_and_base_url(tmp_path: Path) -> None:
    original = Path("config/settings.yaml").read_text(encoding="utf-8")
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        original.replace("  model: ox-alpha-free\n", "  model: null\n", 1),
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match=r"llm\.model"):
        load_settings(config_path)
