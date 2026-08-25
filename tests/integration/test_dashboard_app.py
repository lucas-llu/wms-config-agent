from __future__ import annotations

import gc
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from streamlit.testing.v1 import AppTest

from core.types import Chunk, ChunkRecord
from ingestion import DeleteResult, DocumentInfo
from ingestion.embedding import SparseEncoder
from ingestion.storage import BM25Indexer, ImageStorage
from libs.vector_store import ChromaStore
from observability.dashboard.services import (
    IngestionService,
    TraceService,
    get_dashboard_services,
    get_evaluation_service,
    get_ingestion_service,
)
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
    settings["observability"]["trace_file"] = str(tmp_path / "logs" / "traces.jsonl")
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    bm25_path = tmp_path / "bm25"
    monkeypatch.setenv("WMS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("WMS_BM25_PATH", str(bm25_path))
    monkeypatch.setenv("WMS_INGESTION_HISTORY_PATH", str(tmp_path / "history.db"))
    monkeypatch.setenv("WMS_STAGING_PATH", str(tmp_path / "staging"))
    monkeypatch.setenv("WMS_PROCESSED_PATH", str(tmp_path / "processed"))

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
    get_evaluation_service.cache_clear()
    get_ingestion_service.cache_clear()


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


class _IngestionPageService:
    def __init__(self, *, documents: list[DocumentInfo] | None = None) -> None:
        self.documents = documents or []
        self.ingested: list[tuple[str, bytes, str, bool]] = []
        self.deleted: list[tuple[str, str]] = []

    @staticmethod
    def bounded_progress(stage: str, current: int, total: int):
        return IngestionService.bounded_progress(stage, current, total)

    def ingest_pdf(self, filename, payload, collection, *, force, on_progress):
        self.ingested.append((filename, payload, collection, force))
        on_progress("load", 1, 1)
        on_progress("upsert", 2, 2)
        return SimpleNamespace(
            source_path=filename,
            collection=collection,
            indexing=SimpleNamespace(total_chunks=2, vector_count=2, bm25_count=2),
            skipped=False,
            trace_id="trace-dashboard",
        )

    def list_documents(self):
        return self.documents

    @staticmethod
    def deletion_phrase(document):
        return IngestionService.deletion_phrase(document)

    def delete_document(self, doc_id, *, confirmation):
        self.deleted.append((doc_id, confirmation))
        document = self.documents[0]
        return DeleteResult(document.source_path, document.collection, 2, 2, 0, 1, 1)


class _UnavailableTraceService:
    @staticmethod
    def list_traces(*args, **kwargs):
        del args, kwargs
        raise OSError("trace provider unavailable")


class _FailingIngestionPageService(_IngestionPageService):
    def ingest_pdf(self, filename, payload, collection, *, force, on_progress):
        del filename, payload, collection, force, on_progress
        raise RuntimeError("embedding provider unavailable")


class _EvaluationPageService:
    option = SimpleNamespace(
        identifier="safe-fingerprint",
        name="Sanitized fixture",
        description="Synthetic release benchmark",
        case_count=2,
        fingerprint="safe-fingerprint",
    )
    report = SimpleNamespace(
        dataset_fingerprint="safe-fingerprint",
        passed=True,
        metrics={
            "hit_at_1": 1.0,
            "hit_at_3": 1.0,
            "hit_at_5": 1.0,
            "mrr_at_5": 1.0,
            "refusal_accuracy": 1.0,
            "evidence_accuracy": 1.0,
            "p95_latency_ms": 4.5,
        },
        thresholds={"hit_at_3_min": 1.0},
        threshold_results={"hit_at_3_min": True},
        category_metrics={"retrieval": {"hit_at_3": 1.0}},
        cases=(SimpleNamespace(case_id="safe-case", passed=True),),
    )

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.runs: list[tuple[str, str | None]] = []

    def list_datasets(self):
        return (self.option,)

    @staticmethod
    def list_reports():
        return ()

    def run(self, identifier: str, *, baseline_identifier: str | None = None):
        self.runs.append((identifier, baseline_identifier))
        if self.failure:
            raise self.failure
        return SimpleNamespace(
            report=self.report,
            comparison=None,
            report_summary=SimpleNamespace(identifier="safe-report.json"),
        )


def _render_ingestion(service: object) -> None:
    from observability.dashboard.pages.ingestion_manager import render

    render(service)  # type: ignore[arg-type]


def _render_ingestion_traces(service: object) -> None:
    from observability.dashboard.pages.ingestion_traces import render

    render(service)  # type: ignore[arg-type]


def _render_query_traces(service: object) -> None:
    from observability.dashboard.pages.query_traces import render

    render(service)  # type: ignore[arg-type]


def _render_evaluation(service: object) -> None:
    from observability.dashboard.pages.evaluation import render

    render(service)  # type: ignore[arg-type]


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


def test_dashboard_write_services_are_isolated_to_explicit_ingestion_factory(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_fixture(tmp_path, monkeypatch)

    service = get_ingestion_service()

    assert service.staging_root == (tmp_path / "staging").resolve()
    assert service.pipeline.indexing_pipeline.vector_store.read_only is False
    assert service.pipeline.indexing_pipeline.bm25_indexer.read_only is False
    assert service.pipeline.corpus_processor.integrity.read_only is False


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


def test_ingestion_page_validates_upload_and_reports_bounded_success() -> None:
    service = _IngestionPageService()
    app = AppTest.from_function(_render_ingestion, args=(service,)).run(timeout=20)

    app.button[0].click().run(timeout=20)
    assert app.error[0].value == "Select a PDF before starting ingestion."

    app.file_uploader[0].upload("fixture.pdf", b"%PDF-1.4\nfixture", "application/pdf")
    app.text_input[0].input("dashboard-fixture")
    app.button[0].click().run(timeout=20)

    assert not app.exception
    assert app.success[0].value == "Indexed fixture.pdf into dashboard-fixture."
    assert [metric.value for metric in app.metric[:3]] == ["2", "2", "2"]
    assert service.ingested[0][:3] == (
        "fixture.pdf",
        b"%PDF-1.4\nfixture",
        "dashboard-fixture",
    )


def test_ingestion_page_keeps_deletion_disabled_until_confirmation_matches() -> None:
    document = DocumentInfo(
        "0123456789abcdef",
        "staging/fixture.pdf",
        "dashboard-fixture",
        2,
        0,
        None,
        "fixture-hash",
        "Fixture",
    )
    service = _IngestionPageService(documents=[document])
    app = AppTest.from_function(_render_ingestion, args=(service,)).run(timeout=20)

    assert app.button[1].disabled is True
    app.text_input[1].input("DELETE 0123456789ab").run(timeout=20)
    assert app.button[1].disabled is False
    app.button[1].click().run(timeout=20)

    assert not app.exception
    assert app.success[0].value.startswith("Document deleted:")
    assert service.deleted == [(document.doc_id, "DELETE 0123456789ab")]


def test_ingestion_page_reports_provider_failure_without_crashing() -> None:
    app = AppTest.from_function(
        _render_ingestion,
        args=(_FailingIngestionPageService(),),
    ).run(timeout=20)
    app.file_uploader[0].upload("fixture.pdf", b"%PDF-1.4\nfixture", "application/pdf")
    app.text_input[0].input("dashboard-fixture")
    app.button[0].click().run(timeout=20)

    assert not app.exception
    assert app.error[0].value.startswith("Ingestion failed: RuntimeError")


def test_trace_pages_filter_records_and_tolerate_malformed_lines(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"
    values = [
        _dashboard_trace("ingestion-ok", "ingestion", "ok"),
        _dashboard_trace("ingestion-error", "ingestion", "error"),
        _dashboard_trace("query-ok", "query", "ok"),
    ]
    trace_path.write_text(
        "\n".join(json.dumps(value) for value in values) + "\n{malformed\n",
        encoding="utf-8",
    )
    service = TraceService(trace_path)
    ingestion_app = AppTest.from_function(
        _render_ingestion_traces,
        args=(service,),
    ).run(timeout=20)

    assert not ingestion_app.exception
    assert ingestion_app.warning[0].value.startswith("Ignored 1 malformed")
    assert len(ingestion_app.dataframe[0].value) == 2
    ingestion_app.selectbox[0].select("error").run(timeout=20)
    assert len(ingestion_app.dataframe[0].value) == 1
    assert ingestion_app.dataframe[0].value["Status"].tolist() == ["error"]

    query_app = AppTest.from_function(_render_query_traces, args=(service,)).run(timeout=20)
    assert not query_app.exception
    assert [metric.value for metric in query_app.metric[1:4]] == ["2", "1", "1"]
    assert query_app.metric[4].value == "Yes"


def test_trace_pages_report_provider_failures_without_crashing() -> None:
    ingestion_app = AppTest.from_function(
        _render_ingestion_traces,
        args=(_UnavailableTraceService(),),
    ).run(timeout=20)
    query_app = AppTest.from_function(
        _render_query_traces,
        args=(_UnavailableTraceService(),),
    ).run(timeout=20)

    assert not ingestion_app.exception
    assert ingestion_app.error[0].value.startswith("Ingestion traces are unavailable: OSError")
    assert not query_app.exception
    assert query_app.error[0].value.startswith("Query traces are unavailable: OSError")


def test_evaluation_page_runs_approved_dataset_and_renders_release_metrics() -> None:
    service = _EvaluationPageService()
    app = AppTest.from_function(_render_evaluation, args=(service,)).run(timeout=20)

    assert not app.exception
    assert [item.label for item in app.selectbox] == ["Dataset", "Baseline"]
    app.button[0].click().run(timeout=20)

    assert not app.exception
    assert app.success[0].value.startswith("Benchmark passed")
    assert [metric.value for metric in app.metric[:7]] == [
        "100.0%",
        "100.0%",
        "100.0%",
        "100.0%",
        "100.0%",
        "100.0%",
        "4.5 ms",
    ]
    assert service.runs == [("safe-fingerprint", None)]


def test_evaluation_page_reports_provider_failure_without_crashing() -> None:
    app = AppTest.from_function(
        _render_evaluation,
        args=(_EvaluationPageService(failure=RuntimeError("index unavailable")),),
    ).run(timeout=20)
    app.button[0].click().run(timeout=20)

    assert not app.exception
    assert app.error[0].value.startswith("Evaluation failed: RuntimeError")


def _dashboard_trace(trace_id: str, trace_type: str, status: str) -> dict[str, object]:
    attributes = (
        {"source_name": "fixture.pdf", "collection": "dashboard-fixture"}
        if trace_type == "ingestion"
        else {"query": "putaway fixture", "collection": "dashboard-fixture"}
    )
    stages = (
        [
            {
                "name": "load",
                "elapsed_ms": 1.0,
                "details": {"provider": "fixture", "status": status},
            },
            {
                "name": "upsert",
                "elapsed_ms": 2.0,
                "details": {"record_count": 2},
            },
        ]
        if trace_type == "ingestion"
        else [
            {
                "name": "dense_retrieval",
                "elapsed_ms": 1.0,
                "details": {"result_count": 2},
            },
            {
                "name": "sparse_retrieval",
                "elapsed_ms": 1.0,
                "details": {"result_count": 1},
            },
            {
                "name": "fusion",
                "elapsed_ms": 1.0,
                "details": {
                    "result_count": 1,
                    "rankings": {"final": [{"chunk_id": "fixture-1", "score": 0.9}]},
                },
            },
            {
                "name": "rerank",
                "elapsed_ms": 1.0,
                "details": {"fallback_used": True},
            },
        ]
    )
    return {
        "trace_id": trace_id,
        "trace_type": trace_type,
        "started_at": "2026-08-25T00:00:00+00:00",
        "finished_at": "2026-08-25T00:00:01+00:00",
        "total_elapsed_ms": 12.5,
        "status": status,
        "attributes": attributes,
        "stages": stages,
    }
