from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from core.types import Chunk
from ingestion import IndexingPipeline
from ingestion.storage import BM25Indexer
from libs.embedding import LocalLSAEmbedding
from libs.vector_store import ChromaStore

pytestmark = [pytest.mark.integration, pytest.mark.e2e]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _chunk(chunk_id: str, text: str, code: str) -> Chunk:
    return Chunk(
        id=chunk_id,
        text=text,
        metadata={
            "source_path": f"private/{chunk_id}.pdf",
            "source_relative_path": f"Inbound/{chunk_id}.pdf",
            "file_hash": chunk_id,
            "title": chunk_id.replace("-", " ").title(),
            "collection": "test-corpus",
            "domain": "Inbound",
            "process_code": code,
            "process_stage": "I3.Putaway",
            "document_type": "configuration",
            "page_start": 1,
            "page_end": 1,
        },
        start_offset=0,
        end_offset=len(text),
        source_ref=chunk_id,
    )


def test_mcp_stdio_handshake_discovery_and_cited_query(tmp_path) -> None:
    chunks = [
        _chunk(
            "directed-putaway",
            "SWL.I.11.01 configures directed putaway policy and storage location rules.",
            "SWL.I.11.01",
        ),
        _chunk(
            "appointment",
            "SWL.I.01.01 configures inbound appointment capacity and dock schedule.",
            "SWL.I.01.01",
        ),
        _chunk(
            "cycle-count",
            "SWL.S.04.01 configures inventory cycle count plans and tolerances.",
            "SWL.S.04.01",
        ),
    ]
    model_path = tmp_path / "models"
    chroma_path = tmp_path / "chroma"
    bm25_path = tmp_path / "bm25"
    embedding = LocalLSAEmbedding(dimensions=2, cache_dir=model_path)
    store = ChromaStore(persist_path=chroma_path, collection_name="test_chunks")
    IndexingPipeline(
        embedding=embedding,
        vector_store=store,
        bm25_indexer=BM25Indexer(bm25_path),
        batch_size=2,
    ).index(chunks)

    chunks_path = tmp_path / "chunks"
    chunks_path.mkdir()
    (chunks_path / "test.jsonl").write_text(
        "\n".join(chunk.to_json() for chunk in chunks) + "\n", encoding="utf-8"
    )
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "project": {"name": "test", "environment": "test"},
                "llm": {"provider": "disabled", "model": None},
                "embedding": {
                    "provider": "local_lsa",
                    "model": "tfidf-svd",
                    "dimensions": 2,
                    "batch_size": 2,
                    "cache_dir": str(model_path),
                },
                "splitter": {
                    "provider": "recursive",
                    "chunk_size": 500,
                    "chunk_overlap": 50,
                },
                "vector_store": {
                    "backend": "chroma",
                    "persist_path": str(chroma_path),
                    "collection_name": "test_chunks",
                },
                "retrieval": {
                    "sparse_backend": "bm25",
                    "fusion_algorithm": "rrf",
                    "top_k_dense": 3,
                    "top_k_sparse": 3,
                    "top_k_final": 2,
                    "rrf_k": 60,
                    "max_chunks_per_document": 2,
                    "min_fused_score": 0.02,
                },
                "rerank": {"backend": "none", "model": None, "top_m": 3},
                "evaluation": {
                    "backends": ["custom"],
                    "golden_test_set": str(tmp_path / "golden.json"),
                },
                "observability": {
                    "enabled": True,
                    "trace_file": str(tmp_path / "traces.jsonl"),
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "query_wms_knowledge",
                "arguments": {"query": "SWL.I.11.01 directed putaway configuration"},
            },
        },
    ]
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "start_mcp_server.py"),
            "--settings",
            str(settings_path),
            "--bm25-path",
            str(bm25_path),
            "--chunks",
            str(chunks_path),
        ],
        input="\n".join(json.dumps(message) for message in messages) + "\n",
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=PROJECT_ROOT,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(responses) == 3
    assert len(responses[1]["result"]["tools"]) == 3
    result = responses[2]["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["citations"][0]["process_code"] == "SWL.I.11.01"
    assert "source_path" not in result["structuredContent"]["citations"][0]["metadata"]
    assert result["structuredContent"]["diagnostics"]["trace_id"]
    assert (tmp_path / "traces.jsonl").is_file()
