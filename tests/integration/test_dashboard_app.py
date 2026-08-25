from __future__ import annotations

import gc
import os
import sqlite3
from pathlib import Path

import pytest
import yaml
from streamlit.testing.v1 import AppTest

from core.types import Chunk, ChunkRecord
from ingestion import DocumentInfo
from ingestion.embedding import SparseEncoder
from ingestion.storage import BM25Indexer, ImageStorage
from libs.vector_store import ChromaStore
from observability.dashboard.services import get_dashboard_services
from scripts.verify_dashboard_readonly import (
    changed_nodes,
    configured_storage_paths,
    exercise_dashboard_reads,
    snapshot_storage,
    verify_dashboard_reads,
)

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).parents[2]


def _configure_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_document: bool = True,
    include_second_collection: bool = False,
    include_image: bool = False,
) -> None:
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

    if include_document:
        metadata = {
            "source_path": "fixtures/manual.pdf",
            "collection": "dashboard-fixture",
            "file_hash": "fixture-hash",
            "title": "Dashboard fixture",
            "images": [],
        }
        records = [ChunkRecord("fixture-1", "MOCA configuration", metadata, [1.0, 0.0])]
        chunks = [Chunk("fixture-1", "MOCA configuration", metadata, 0, 18)]
        if include_second_collection:
            second_metadata = {
                **metadata,
                "source_path": "fixtures/inbound.pdf",
                "collection": "inbound-fixture",
                "file_hash": "inbound-hash",
                "title": "Inbound fixture",
            }
            records.append(
                ChunkRecord("fixture-2", "Inbound appointment", second_metadata, [0.0, 1.0])
            )
            chunks.append(Chunk("fixture-2", "Inbound appointment", second_metadata, 0, 19))
        ChromaStore(persist_path=tmp_path / "chroma", collection_name="dashboard_chunks").upsert(
            records
        )
        BM25Indexer(bm25_path).build(SparseEncoder().encode(chunks))
        if include_image:
            image_database = tmp_path / "images.db"
            ImageStorage(tmp_path / "images", image_database).save_bytes(
                "fixture-image",
                _ONE_PIXEL_PNG,
                collection="dashboard-fixture",
                extension=".png",
                doc_hash="fixture-hash",
                page_num=1,
            )
            connection = sqlite3.connect(image_database)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            gc.collect()
    get_dashboard_services.cache_clear()


_ONE_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _render_data_browser(data_service: object) -> None:
    from observability.dashboard.pages.data_browser import render

    render(data_service)  # type: ignore[arg-type]


class _UnavailableDataService:
    def list_collections(self) -> list[str]:
        raise RuntimeError("corrupt fixture store")


class _BrokenDetailDataService:
    document = DocumentInfo(
        "broken", "fixtures/broken.pdf", "broken", 1, 0, None, "broken-hash", "Broken"
    )

    def list_collections(self) -> list[str]:
        return ["broken"]

    def list_documents(self, collection: str | None = None) -> list[DocumentInfo]:
        del collection
        return [self.document]

    def document_rows(self, collection: str | None = None) -> list[dict[str, object]]:
        del collection
        return [{"Title": "Broken", "Collection": "broken"}]

    def get_document_detail(self, doc_id: str) -> None:
        del doc_id
        raise RuntimeError("damaged metadata")


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


def test_dashboard_pages_render_empty_store_without_failure(tmp_path: Path, monkeypatch) -> None:
    _configure_fixture(tmp_path, monkeypatch, include_document=False)
    overview = _PROJECT_ROOT / "src" / "observability" / "dashboard" / "pages" / "overview.py"
    browser = _PROJECT_ROOT / "src" / "observability" / "dashboard" / "pages" / "data_browser.py"

    overview_app = AppTest.from_file(str(overview)).run(timeout=20)
    browser_app = AppTest.from_file(str(browser)).run(timeout=20)

    assert not overview_app.exception
    assert [metric.value for metric in overview_app.metric[:4]] == ["0", "0", "0", "0"]
    assert not browser_app.exception
    assert browser_app.info[0].value == "No indexed documents were found for this collection."


def test_overview_recovers_from_missing_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WMS_CONFIG_PATH", str(tmp_path / "missing-settings.yaml"))
    get_dashboard_services.cache_clear()
    page = _PROJECT_ROOT / "src" / "observability" / "dashboard" / "pages" / "overview.py"

    app = AppTest.from_file(str(page)).run(timeout=20)

    assert not app.exception
    assert app.error[0].value.startswith("Dashboard services are unavailable: SettingsError")
    assert app.info[0].value == "Check the settings and local index paths, then reload this page."


def test_data_browser_recovers_from_store_and_detail_failures() -> None:
    unavailable = AppTest.from_function(
        _render_data_browser, args=(_UnavailableDataService(),)
    ).run(timeout=20)
    broken_detail = AppTest.from_function(
        _render_data_browser, args=(_BrokenDetailDataService(),)
    ).run(timeout=20)

    assert not unavailable.exception
    assert unavailable.error[0].value.startswith("Knowledge-base data is unavailable: RuntimeError")
    assert not broken_detail.exception
    assert broken_detail.error[0].value.startswith(
        "Document details could not be loaded: RuntimeError"
    )


def test_data_browser_filters_collections_and_previews_managed_image(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_fixture(
        tmp_path,
        monkeypatch,
        include_second_collection=True,
        include_image=True,
    )
    page = _PROJECT_ROOT / "src" / "observability" / "dashboard" / "pages" / "data_browser.py"

    app = AppTest.from_file(str(page)).run(timeout=20)

    assert not app.exception
    assert len(app.dataframe) == 1
    assert app.dataframe[0].value["Collection"].tolist() == [
        "dashboard-fixture",
        "inbound-fixture",
    ]
    assert len(app.image) == 1
    assert app.image[0].proto.imgs[0].caption == "fixture-image · page 1"

    app.selectbox[0].select("inbound-fixture").run(timeout=20)

    assert not app.exception
    assert len(app.dataframe) == 1
    assert app.selectbox[0].value == "inbound-fixture"
    assert app.dataframe[0].value["Collection"].tolist() == ["inbound-fixture"]
    assert app.expander[0].label.startswith("Chunk 1 · fixture-2")
    assert len(app.image) == 0


def test_dashboard_navigation_reaches_all_six_pages(tmp_path: Path, monkeypatch) -> None:
    _configure_fixture(tmp_path, monkeypatch, include_document=False)
    app_path = _PROJECT_ROOT / "src" / "observability" / "dashboard" / "app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=20)
    pages = [
        ("pages/overview.py", "WMS Config Agent"),
        ("pages/data_browser.py", "Data browser"),
        ("pages/ingestion_manager.py", "Ingestion"),
        ("pages/ingestion_traces.py", "Ingestion traces"),
        ("pages/query_traces.py", "Query traces"),
        ("pages/evaluation.py", "Evaluation"),
    ]

    for page_path, title in pages:
        app.switch_page(page_path).run(timeout=20)
        assert not app.exception
        assert app.title[0].value == title


def test_dashboard_management_reads_leave_fixture_stores_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_fixture(tmp_path, monkeypatch, include_image=True)
    settings_path = Path(os.environ["WMS_CONFIG_PATH"])
    paths = configured_storage_paths(settings_path)
    before = snapshot_storage(paths)

    summary = exercise_dashboard_reads(settings_path)
    after = snapshot_storage(paths)

    assert summary["documents"] == 1
    assert summary["dense_chunks"] == 1
    assert summary["sample_previewable_images"] == 1
    assert changed_nodes(before, after) == []


def test_dashboard_factory_composes_only_read_only_storage_adapters(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_fixture(tmp_path, monkeypatch)

    manager = get_dashboard_services().data.document_manager

    assert manager.chroma_store.read_only is True
    assert manager.bm25_indexer.read_only is True
    assert manager.image_storage.read_only is True
    assert manager.file_integrity.read_only is True


def test_dashboard_readonly_verifier_does_not_initialize_missing_stores(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_fixture(tmp_path, monkeypatch, include_document=False)
    settings_path = Path(os.environ["WMS_CONFIG_PATH"])
    paths = configured_storage_paths(settings_path)

    summary = verify_dashboard_reads(settings_path)

    assert summary["documents"] == 0
    assert all(not path.exists() for path in paths.values())


def test_dashboard_readonly_snapshot_detects_parent_directory_creation(tmp_path: Path) -> None:
    missing_database = tmp_path / "new-parent" / "history.db"
    paths = {"history": missing_database}
    before = snapshot_storage(paths)

    missing_database.parent.mkdir()

    changes = changed_nodes(before, snapshot_storage(paths))
    assert "history::parent:0" in changes


def test_dashboard_readonly_snapshot_detects_rollback_journal_creation(tmp_path: Path) -> None:
    database = tmp_path / "history.db"
    database.write_bytes(b"fixture")
    paths = {"history": database}
    before = snapshot_storage(paths)

    Path(f"{database}-journal").write_bytes(b"active writer")

    changes = changed_nodes(before, snapshot_storage(paths))
    assert "history-journal" in changes
