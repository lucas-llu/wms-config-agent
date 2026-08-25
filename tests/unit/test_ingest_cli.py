from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.settings import load_settings
from scripts import ingest


def test_pdf_mode_requires_existing_pdf_and_collection(tmp_path: Path) -> None:
    pdf = tmp_path / "manual.PDF"
    pdf.write_bytes(b"%PDF-1.4 fixture")

    args = ingest.parse_args(["--path", str(pdf), "--collection", " customer-a "])

    assert args.path == pdf
    assert args.collection == "customer-a"

    with pytest.raises(SystemExit):
        ingest.parse_args(["--path", str(pdf)])
    with pytest.raises(SystemExit):
        ingest.parse_args(["--path", str(tmp_path / "missing.pdf"), "--collection", "test"])


def test_pdf_and_chunk_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")

    with pytest.raises(SystemExit):
        ingest.parse_args(
            [
                "--path",
                str(pdf),
                "--collection",
                "test",
                "--chunks",
                str(tmp_path / "chunks"),
            ]
        )
    with pytest.raises(SystemExit):
        ingest.parse_args(["--collection", "test"])


def test_default_and_explicit_chunk_modes_keep_legacy_routing(monkeypatch) -> None:
    settings = load_settings()
    calls: list[Path | None] = []
    monkeypatch.setattr(
        ingest,
        "_index_preprocessed_chunks",
        lambda args, unused_settings: calls.append(args.chunks) or {"mode": "chunks"},
    )

    default_result = ingest._run(ingest.parse_args([]), settings)
    explicit_result = ingest._run(ingest.parse_args(["--chunks", "custom/chunks"]), settings)

    assert default_result == {"mode": "chunks"}
    assert explicit_result == {"mode": "chunks"}
    assert calls == [None, Path("custom/chunks")]


def test_pdf_mode_builds_and_runs_complete_ingestion_pipeline(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "staging" / "manual.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.4 fixture")
    output = tmp_path / "processed"
    history = tmp_path / "history.db"
    bm25 = tmp_path / "bm25"
    args = ingest.parse_args(
        [
            "--path",
            str(pdf),
            "--collection",
            "customer-a",
            "--output-root",
            str(output),
            "--history-path",
            str(history),
            "--bm25-path",
            str(bm25),
            "--force",
            "--quiet",
        ]
    )
    settings = load_settings()
    calls: dict[str, object] = {}

    class _Pipeline:
        def run(self, path, **kwargs):
            calls["run"] = (path, kwargs)
            return SimpleNamespace(to_dict=lambda: {"document_id": "fixture"})

    def create_pipeline(received_settings, **kwargs):
        calls["factory"] = (received_settings, kwargs)
        return _Pipeline()

    monkeypatch.setattr(ingest, "create_ingestion_pipeline", create_pipeline)

    result = ingest._ingest_pdf(args, settings)

    assert result == {"document_id": "fixture"}
    received_settings, factory_kwargs = calls["factory"]
    assert received_settings is settings
    assert factory_kwargs == {
        "source_root": pdf.parent.resolve(),
        "output_root": output,
        "history_path": history,
        "bm25_path": bm25,
    }
    run_path, run_kwargs = calls["run"]
    assert run_path == pdf.resolve()
    assert run_kwargs == {
        "collection": "customer-a",
        "on_progress": None,
        "force": True,
    }


def test_chunk_mode_holds_lifecycle_lock_across_snapshot_and_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Lease:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, *unused):
            events.append("exit")

    class _Lock:
        def lease(self):
            return _Lease()

    class _Collector:
        def __init__(self, *args, **kwargs):
            pass

        def start(self, *args, **kwargs):
            return None

        def collect(self, trace):
            pass

    class _Pipeline:
        def __init__(self, **kwargs):
            pass

        def index(self, chunks, **kwargs):
            assert events == ["enter", "load"]
            events.append("index")
            return SimpleNamespace(to_dict=lambda: {"total_chunks": len(chunks)})

    monkeypatch.setattr(
        ingest.LifecycleLock,
        "for_database",
        lambda *args, **kwargs: _Lock(),
    )
    monkeypatch.setattr(
        ingest,
        "load_preprocessed_chunks",
        lambda path: events.append("load") or [object()],
    )
    monkeypatch.setattr(ingest, "IndexingPipeline", _Pipeline)
    monkeypatch.setattr(ingest.EmbeddingFactory, "create", lambda settings: object())
    monkeypatch.setattr(ingest.VectorStoreFactory, "create", lambda settings: object())
    monkeypatch.setattr(ingest, "BM25Indexer", lambda path: object())
    monkeypatch.setattr(ingest, "TraceCollector", _Collector)
    args = ingest.parse_args(
        [
            "--chunks",
            str(tmp_path / "chunks"),
            "--history-path",
            str(tmp_path / "history.db"),
            "--quiet",
        ]
    )

    result = ingest._index_preprocessed_chunks(args, load_settings())

    assert result == {"total_chunks": 1}
    assert events == ["enter", "load", "index", "exit"]
