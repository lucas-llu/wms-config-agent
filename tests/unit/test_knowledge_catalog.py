from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agents.workspace import Workspace
from core.types import Chunk
from libs.loader import SQLiteIntegrityChecker
from mcp_server.catalog import CorpusCatalog
from mcp_server.tool_registry import ToolRegistry
from mcp_server.tools import GetKnowledgeCatalogTool


class IndexCounter:
    def __init__(self, value: int) -> None:
        self.value = value

    def count(self) -> int:
        return self.value


def _write_chunks(path: Path, chunks: list[Chunk]) -> Path:
    path.mkdir(parents=True)
    (path / "chunks.jsonl").write_text(
        "\n".join(chunk.to_json() for chunk in chunks) + "\n",
        encoding="utf-8",
    )
    return path


def _chunk(
    *,
    chunk_id: str,
    file_hash: str,
    text: str,
    source_path: str,
    source_relative_path: str | None,
    version: str,
    module: str,
    site: str,
    environment: str,
) -> Chunk:
    metadata = {
        "file_hash": file_hash,
        "title": f"Title {chunk_id}",
        "collection": "system-training",
        "domain": module,
        "process_code": f"PROC-{chunk_id}",
        "document_type": "configuration",
        "page_count": 3,
        "source_path": source_path,
        "version": version,
        "module": module,
        "site": site,
        "environment": environment,
    }
    if source_relative_path is not None:
        metadata["source_relative_path"] = source_relative_path
    return Chunk(
        id=chunk_id,
        text=text,
        metadata=metadata,
        start_offset=0,
        end_offset=len(text),
    )


def test_catalog_is_privacy_safe_and_reports_scope_and_index_health(tmp_path: Path) -> None:
    chunks = [
        _chunk(
            chunk_id="chunk-private",
            file_hash="hash-private",
            text="PRIVATE BODY MUST NOT APPEAR",
            source_path=str(tmp_path / "private" / "manual.pdf"),
            source_relative_path=None,
            version="unspecified",
            module="inbound",
            site="unspecified",
            environment="test",
        ),
        _chunk(
            chunk_id="chunk-complete",
            file_hash="hash-complete",
            text="Another private body",
            source_path=str(tmp_path / "other.pdf"),
            source_relative_path="Inbound/putaway.pdf",
            version="2024.1",
            module="inbound",
            site="DC01",
            environment="test",
        ),
    ]
    catalog = CorpusCatalog(
        _write_chunks(tmp_path / "chunks", chunks),
        dense_index=IndexCounter(2),
        sparse_index=IndexCounter(2),
    )

    payload = catalog.knowledge_snapshot()
    serialized = json.dumps(payload)

    assert payload["summary"]["document_count"] == 2
    assert payload["summary"]["scope_incomplete_count"] == 1
    assert payload["index_health"] == {
        "status": "healthy",
        "scope": "shared_index",
        "dense_count": 2,
        "sparse_count": 2,
        "processed_chunk_count": 2,
        "aligned": True,
        "checked_at": payload["index_health"]["checked_at"],
    }
    assert "PRIVATE BODY MUST NOT APPEAR" not in serialized
    assert "Another private body" not in serialized
    assert str(tmp_path) not in serialized

    private_document = next(
        item for item in payload["documents"] if item["document_id"] == "doc-hash-private"
    )
    assert private_document["source"] == "manual.pdf"
    assert private_document["scope"] == {
        "version": None,
        "module": "inbound",
        "site": None,
        "environment": "test",
        "status": "incomplete",
        "missing_fields": ["version", "site"],
    }
    assert private_document["freshness"]["status"] == "unknown"

    complete_document = next(
        item for item in payload["documents"] if item["document_id"] == "doc-hash-complete"
    )
    assert complete_document["source"] == "Inbound/putaway.pdf"
    assert complete_document["scope"]["status"] == "complete"


def test_catalog_marks_newer_source_as_stale_and_filters_records(tmp_path: Path) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_text("private", encoding="utf-8")
    history_path = tmp_path / "history.db"
    SQLiteIntegrityChecker(history_path).mark_success(
        "hash-stale",
        source_path,
        collection="system-training",
    )
    future = time.time() + 60
    os.utime(source_path, (future, future))
    chunks = [
        _chunk(
            chunk_id="chunk-stale",
            file_hash="hash-stale",
            text="stale private body",
            source_path=str(source_path),
            source_relative_path="Inbound/stale.pdf",
            version="2024.1",
            module="inbound",
            site="DC01",
            environment="test",
        ),
        _chunk(
            chunk_id="chunk-unknown",
            file_hash="hash-unknown",
            text="unknown private body",
            source_path=str(tmp_path / "unknown.pdf"),
            source_relative_path="Outbound/unknown.pdf",
            version="2024.1",
            module="outbound",
            site="DC01",
            environment="test",
        ),
    ]
    catalog = CorpusCatalog(
        _write_chunks(tmp_path / "chunks", chunks),
        history_path=history_path,
    )

    stale = catalog.knowledge_snapshot(freshness_status="stale")
    assert stale["pagination"] == {"offset": 0, "limit": 100, "returned": 1, "total": 1}
    assert stale["documents"][0]["document_id"] == "doc-hash-stale"
    assert stale["documents"][0]["freshness"] == {
        "status": "stale",
        "processed_at": stale["documents"][0]["freshness"]["processed_at"],
        "reason": "source_newer_than_index",
    }

    inbound = catalog.knowledge_snapshot(module="inbound", scope_status="complete", limit=1)
    assert inbound["pagination"] == {"offset": 0, "limit": 1, "returned": 1, "total": 1}
    assert inbound["documents"][0]["scope"]["module"] == "inbound"


def test_knowledge_catalog_tool_validates_arguments_and_uses_tool_contract(
    tmp_path: Path,
) -> None:
    chunk = _chunk(
        chunk_id="chunk-1",
        file_hash="hash-1",
        text="private body",
        source_path=str(tmp_path / "manual.pdf"),
        source_relative_path="Inbound/manual.pdf",
        version="2024.1",
        module="inbound",
        site="DC01",
        environment="test",
    )
    catalog = CorpusCatalog(_write_chunks(tmp_path / "chunks", [chunk]))
    tool = GetKnowledgeCatalogTool(catalog).definition()
    registry = ToolRegistry([tool])

    result = registry.call("get_wms_knowledge_catalog", {"module": "inbound", "limit": 1})
    assert result["isError"] is False
    assert result["structuredContent"]["pagination"]["returned"] == 1
    assert result["structuredContent"]["documents"][0]["source"] == "Inbound/manual.pdf"
    assert tool.output_schema is not None
    assert tool.output_schema["additionalProperties"] is False
    assert tool.output_schema["properties"]["documents"]["items"]["additionalProperties"] is False

    invalid = registry.call("get_wms_knowledge_catalog", {"scope_status": "missing"})
    assert invalid["isError"] is True
    assert invalid["structuredContent"]["tool"] == "get_wms_knowledge_catalog"


def test_workspace_catalog_does_not_leak_global_index_counts(tmp_path: Path) -> None:
    allowed = _chunk(
        chunk_id="chunk-a",
        file_hash="hash-a",
        text="allowed private body",
        source_path=str(tmp_path / "a.pdf"),
        source_relative_path="a.pdf",
        version="2024.1",
        module="inbound",
        site="DC01",
        environment="test",
    )
    allowed.metadata["collection"] = "a"
    denied = _chunk(
        chunk_id="chunk-b",
        file_hash="hash-b",
        text="denied private body",
        source_path=str(tmp_path / "b.pdf"),
        source_relative_path="b.pdf",
        version="2024.1",
        module="inbound",
        site="DC01",
        environment="test",
    )
    denied.metadata["collection"] = "b"
    policy = Workspace(
        "workspace:a",
        "A",
        ("a",),
        ("inbound",),
        ("DC01",),
        ("test",),
    )
    catalog = CorpusCatalog(
        _write_chunks(tmp_path / "chunks", [allowed, denied]),
        workspace=policy,
        dense_index=IndexCounter(999),
        sparse_index=IndexCounter(999),
    )

    snapshot = catalog.knowledge_snapshot()

    assert snapshot["summary"]["document_count"] == 1
    assert snapshot["documents"][0]["document_id"] == "doc-hash-a"
    assert snapshot["index_health"]["status"] == "unverified"
    assert snapshot["index_health"]["dense_count"] is None
    assert snapshot["index_health"]["sparse_count"] is None
    assert "999" not in json.dumps(snapshot)
