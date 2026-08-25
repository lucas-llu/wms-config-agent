from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from observability.dashboard.services import TraceService


def _trace(
    trace_id: str,
    trace_type: str,
    *,
    status: str = "ok",
    subject: str = "fixture",
) -> dict[str, object]:
    attributes = (
        {"source_name": f"{subject}.pdf", "collection": "manuals"}
        if trace_type == "ingestion"
        else {
            "query": subject,
            "collection": "manuals",
            "api_key": "should-never-render",
        }
    )
    return {
        "trace_id": trace_id,
        "trace_type": trace_type,
        "started_at": f"2026-08-25T00:00:0{trace_id[-1]}+00:00",
        "finished_at": "2026-08-25T00:00:10+00:00",
        "total_elapsed_ms": 12.5,
        "status": status,
        "attributes": attributes,
        "stages": [
            {
                "name": "dense_retrieval" if trace_type == "query" else "load",
                "elapsed_ms": 3.2,
                "details": {
                    "status": status,
                    "result_count": 2,
                    "content": "private document body",
                    "provider": "fixture",
                },
            },
            {
                "name": "fusion" if trace_type == "query" else "upsert",
                "elapsed_ms": 4.0,
                "details": {
                    "result_count": 1,
                    "rankings": {"final": [{"chunk_id": "chunk-1", "score": 0.9}]},
                    "authorization": "Bearer legacy-secret",
                },
            },
            {
                "name": "rerank" if trace_type == "query" else "history_commit",
                "elapsed_ms": 1.0,
                "details": {"fallback_used": trace_type == "query"},
            },
        ],
    }


def _write_lines(path: Path, values: list[object]) -> None:
    path.write_text(
        "\n".join(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for value in values
        )
        + "\n",
        encoding="utf-8",
    )


def test_trace_service_skips_malformed_lines_and_filters_recent_records(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    _write_lines(
        path,
        [
            _trace("trace-1", "ingestion", subject="putaway"),
            "{broken-json",
            _trace("trace-2", "query", subject="dock capacity"),
            _trace("trace-3", "query", status="error", subject="cycle count"),
            {"unsupported": True},
        ],
    )
    service = TraceService(path)

    result = service.list_traces("query", status="error", search="cycle")

    assert [record.trace_id for record in result.records] == ["trace-3"]
    assert result.malformed_lines == 2
    assert result.truncated is False


def test_trace_service_exposes_safe_diagnostics_without_bodies_or_credentials(
    tmp_path: Path,
) -> None:
    path = tmp_path / "traces.jsonl"
    payload = _trace("trace-2", "query", subject="api_key=private-value putaway")
    _write_lines(path, [payload])
    service = TraceService(path)

    record = service.list_traces("query").records[0]
    diagnostics = service.query_diagnostics(record)
    serialized = json.dumps(asdict(record))

    assert "private-value" not in serialized
    assert "should-never-render" not in serialized
    assert "legacy-secret" not in serialized
    assert "private document body" not in serialized
    assert diagnostics["dense_count"] == 2
    assert diagnostics["final_count"] == 1
    assert diagnostics["rerank_fallback"] is True
    assert diagnostics["rankings"]["final"][0]["chunk_id"] == "chunk-1"


def test_trace_service_reports_bounded_tail_reads(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    _write_lines(path, [_trace(f"trace-{index}", "ingestion") for index in range(10)])

    result = TraceService(path, max_records=2, max_read_bytes=700).list_traces("ingestion")

    assert len(result.records) <= 2
    assert result.truncated is True
