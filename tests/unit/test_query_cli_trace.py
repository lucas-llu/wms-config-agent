from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from scripts import query as query_cli


class FakeStore:
    @staticmethod
    def count() -> int:
        return 1


class FakeBM25:
    def __init__(self, path) -> None:
        self.path = path

    @staticmethod
    def count() -> int:
        return 1


class FakeHybridSearch:
    def __init__(self, *args) -> None:
        self.args = args

    @staticmethod
    def search_with_details(query, top_k=None, filters=None, trace=None):
        assert trace is not None
        trace.record_stage(
            "query_processing",
            1.0,
            details={"method": "fake", "provider": "test"},
        )
        return SimpleNamespace(results=(), failures={}, evidence_sufficient=False)


class FakeResponse:
    message = "No evidence"
    markdown = "No evidence"

    @staticmethod
    def to_dict():
        return {"diagnostics": {}}


class FakeResponseBuilder:
    @staticmethod
    def build(outcome):
        del outcome
        return FakeResponse()


def test_query_cli_collects_trace_when_observability_is_enabled(
    tmp_path, monkeypatch, capsys
) -> None:
    trace_path = tmp_path / "query-traces.jsonl"
    settings = SimpleNamespace(
        observability=SimpleNamespace(enabled=True, trace_file=trace_path),
        retrieval=SimpleNamespace(rrf_k=60),
    )
    monkeypatch.setattr(query_cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(query_cli.EmbeddingFactory, "create", lambda value: object())
    monkeypatch.setattr(query_cli.VectorStoreFactory, "create", lambda value: FakeStore())
    monkeypatch.setattr(query_cli, "BM25Indexer", FakeBM25)
    monkeypatch.setattr(query_cli, "HybridSearch", FakeHybridSearch)
    monkeypatch.setattr(query_cli, "ResponseBuilder", FakeResponseBuilder)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "query.py",
            "--query",
            "putaway configuration",
            "--collection",
            "fixture",
            "--no-rerank",
            "--json",
        ],
    )

    query_cli.main()

    capsys.readouterr()
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["trace_type"] == "query"
    assert trace["status"] == "ok"
    assert trace["attributes"]["collection"] == "fixture"
    assert trace["stages"][0]["name"] == "query_processing"
