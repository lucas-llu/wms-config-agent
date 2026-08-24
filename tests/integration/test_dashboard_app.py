from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from streamlit.testing.v1 import AppTest

from core.types import Chunk, ChunkRecord
from ingestion.embedding import SparseEncoder
from ingestion.storage import BM25Indexer
from libs.vector_store import ChromaStore
from observability.dashboard.services import get_dashboard_services

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).parents[2]


def _configure_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = yaml.safe_load(
        (_PROJECT_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    )
    settings["vector_store"]["persist_path"] = str(tmp_path / "chroma")
    settings["vector_store"]["collection_name"] = "dashboard_chunks"
    settings["ingestion"]["image_storage"]["root_path"] = str(tmp_path / "images")
    settings["ingestion"]["image_storage"]["database_path"] = str(tmp_path / "images.db")
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    bm25_path = tmp_path / "bm25"
    monkeypatch.setenv("WMS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("WMS_BM25_PATH", str(bm25_path))
    monkeypatch.setenv("WMS_INGESTION_HISTORY_PATH", str(tmp_path / "history.db"))

    metadata = {
        "source_path": "fixtures/manual.pdf",
        "collection": "dashboard-fixture",
        "file_hash": "fixture-hash",
        "title": "Dashboard fixture",
        "images": [],
    }
    ChromaStore(persist_path=tmp_path / "chroma", collection_name="dashboard_chunks").upsert(
        [ChunkRecord("fixture-1", "MOCA configuration", metadata, [1.0, 0.0])]
    )
    BM25Indexer(bm25_path).build(
        SparseEncoder().encode([Chunk("fixture-1", "MOCA configuration", metadata, 0, 18)])
    )
    get_dashboard_services.cache_clear()


def test_overview_page_renders_fixture_index(tmp_path: Path, monkeypatch) -> None:
    _configure_fixture(tmp_path, monkeypatch)
    page = _PROJECT_ROOT / "src" / "observability" / "dashboard" / "pages" / "overview.py"

    app = AppTest.from_file(str(page)).run(timeout=20)

    assert not app.exception
    assert app.title[0].value == "WMS Config Agent"
    assert [metric.value for metric in app.metric[:3]] == ["1", "1", "1"]
    assert app.success[0].value == "Dense and sparse indexes are aligned."


def test_dashboard_shell_runs_default_page(tmp_path: Path, monkeypatch) -> None:
    _configure_fixture(tmp_path, monkeypatch)
    app_path = _PROJECT_ROOT / "src" / "observability" / "dashboard" / "app.py"

    app = AppTest.from_file(str(app_path)).run(timeout=20)

    assert not app.exception
    assert app.title[0].value == "WMS Config Agent"


def test_data_browser_page_renders_documents_and_chunks(tmp_path: Path, monkeypatch) -> None:
    _configure_fixture(tmp_path, monkeypatch)
    page = _PROJECT_ROOT / "src" / "observability" / "dashboard" / "pages" / "data_browser.py"

    app = AppTest.from_file(str(page)).run(timeout=20)

    assert not app.exception
    assert app.title[0].value == "Data browser"
    assert [item.label for item in app.selectbox] == ["Collection", "Document details"]
    assert len(app.dataframe) == 1
    assert app.expander[0].label.startswith("Chunk 1 · fixture-1")
