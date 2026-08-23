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
