from __future__ import annotations

import base64
import json

from core.response import MultimodalAssembler
from core.trace import TraceCollector
from core.types import RetrievalResult


def test_trace_collector_writes_completed_jsonl(tmp_path) -> None:
    path = tmp_path / "traces.jsonl"
    collector = TraceCollector(path)
    trace = collector.start("query", {"query": "putaway"})
    assert trace is not None
    trace.record_stage("dense_retrieval", 12.3456, details={"result_count": 2})
    trace.finish()
    collector.collect(trace)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["trace_id"] == trace.trace_id
    assert payload["status"] == "ok"
    assert payload["stages"][0]["elapsed_ms"] == 12.346


def test_trace_redacts_credentials_and_omits_document_bodies(tmp_path) -> None:
    path = tmp_path / "traces.jsonl"
    collector = TraceCollector(path)
    trace = collector.start(
        "query",
        {
            "query": "find config api_key=do-not-store-this-value",
            "authorization": "Bearer secret-token",
        },
    )
    assert trace is not None
    trace.record_stage(
        "dense_retrieval",
        1.0,
        details={
            "content": "private document body",
            "results": [{"chunk_id": "chunk-1", "score": 0.9}],
        },
    )
    trace.finish()
    collector.collect(trace)

    payload = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert "do-not-store-this-value" not in serialized
    assert "secret-token" not in serialized
    assert "private document body" not in serialized
    assert payload["stages"][0]["details"]["results"][0]["chunk_id"] == "chunk-1"


def test_trace_finish_retains_only_safe_error_categories() -> None:
    safe = TraceCollector("unused.jsonl").start("query")
    assert safe is not None
    safe.finish(status="error", error="TimeoutError")
    assert safe.to_dict()["error"] == "TimeoutError"

    unsafe = TraceCollector("unused.jsonl").start("query")
    assert unsafe is not None
    unsafe.finish(
        status="error",
        error="Bearer private-token while parsing private document body",
    )
    serialized = json.dumps(unsafe.to_dict())
    assert "private-token" not in serialized
    assert "private document body" not in serialized
    assert unsafe.to_dict()["error"] == "[REDACTED]"


def test_multimodal_assembler_only_reads_allowed_images(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image = allowed / "diagram.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    result = RetrievalResult(
        chunk_id="chunk-1",
        score=1.0,
        text="diagram",
        metadata={
            "source_path": "private.pdf",
            "images": [{"path": str(image)}, {"path": str(outside)}],
        },
    )

    blocks = MultimodalAssembler([allowed]).assemble([result])

    assert len(blocks) == 1
    assert blocks[0]["mimeType"] == "image/png"
    assert base64.b64decode(blocks[0]["data"]) == image.read_bytes()
