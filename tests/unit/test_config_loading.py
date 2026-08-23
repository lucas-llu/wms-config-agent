from pathlib import Path

import pytest

from core.settings import SettingsError, load_settings


def test_load_project_settings() -> None:
    settings = load_settings("config/settings.yaml")

    assert settings.project.name == "wms-config-agent"
    assert settings.embedding.provider == "disabled"
    assert settings.vector_store.backend == "memory"
    assert settings.retrieval.top_k_final == 5


def test_missing_required_field_has_readable_path(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        """
project: {name: test, environment: test}
llm: {provider: disabled}
embedding: {}
vector_store: {backend: memory, persist_path: data/db}
retrieval:
  {sparse_backend: bm25, fusion_algorithm: rrf, top_k_dense: 2, top_k_sparse: 2, top_k_final: 2}
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
        original.replace("provider: disabled", "provider: ${TEST_PROVIDER}", 1),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_PROVIDER", "offline")

    assert load_settings(config_path).llm.provider == "offline"
