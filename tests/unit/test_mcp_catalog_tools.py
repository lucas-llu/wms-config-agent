from __future__ import annotations

import json

from core.types import Chunk
from mcp_server.catalog import CorpusCatalog
from mcp_server.tool_registry import ToolRegistry
from mcp_server.tools import GetDocumentSummaryTool, ListCollectionsTool


def _write_chunks(tmp_path) -> CorpusCatalog:
    chunks_path = tmp_path / "chunks"
    chunks_path.mkdir()
    chunks = [
        Chunk(
            id=f"chunk-{index}",
            text=text,
            metadata={
                "source_path": f"private/doc-{index}.pdf",
                "source_relative_path": f"Inbound/doc-{index}.pdf",
                "title": title,
                "collection": "system-training",
                "domain": "Inbound",
                "process_code": process_code,
                "document_type": "configuration",
                "page_count": 5,
            },
            start_offset=0,
            end_offset=len(text),
            source_ref=f"doc-{index}",
        )
        for index, (title, process_code, text) in enumerate(
            [
                ("Putaway", "SWL.I.11.01", "Configure putaway policy."),
                ("Appointment", "SWL.I.01.01", "Configure appointments."),
            ],
            start=1,
        )
    ]
    (chunks_path / "sample.jsonl").write_text(
        "\n".join(chunk.to_json() for chunk in chunks) + "\n", encoding="utf-8"
    )
    return CorpusCatalog(chunks_path)


def test_catalog_tools_list_and_summarize_documents(tmp_path) -> None:
    catalog = _write_chunks(tmp_path)
    registry = ToolRegistry(
        [
            ListCollectionsTool(catalog).definition(),
            GetDocumentSummaryTool(catalog).definition(),
        ]
    )

    collections = registry.call("list_wms_collections", {})
    summary = registry.call("get_wms_document_summary", {"document_id": "SWL.I.11.01"})

    assert collections["structuredContent"]["collections"][0]["chunk_count"] == 2
    document = summary["structuredContent"]["documents"][0]
    assert document["title"] == "Putaway"
    assert document["excerpt"] == "Configure putaway policy."


def test_document_summary_returns_tool_error_when_not_found(tmp_path) -> None:
    catalog = _write_chunks(tmp_path)
    registry = ToolRegistry([GetDocumentSummaryTool(catalog).definition()])

    result = registry.call("get_wms_document_summary", {"document_id": "missing"})

    assert result["isError"] is True
    assert json.loads(json.dumps(result))["structuredContent"]["tool"] == (
        "get_wms_document_summary"
    )
