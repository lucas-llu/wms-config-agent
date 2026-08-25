"""Application composition for the local WMS MCP server."""

from __future__ import annotations

from pathlib import Path

from core.query_engine import (
    DenseRetriever,
    HybridSearch,
    QueryProcessor,
    ReciprocalRankFusion,
    SafeReranker,
    SparseRetriever,
)
from core.response import MultimodalAssembler, ResponseBuilder
from core.settings import load_settings
from core.trace import TraceCollector
from ingestion.storage import BM25Indexer
from libs.embedding import EmbeddingFactory
from libs.reranker import RerankerFactory
from libs.vector_store import VectorStoreFactory
from mcp_server.catalog import CorpusCatalog
from mcp_server.protocol_handler import ProtocolHandler
from mcp_server.tool_registry import ToolRegistry
from mcp_server.tools import (
    GetDocumentSummaryTool,
    ListCollectionsTool,
    QueryKnowledgeHubTool,
)


def create_protocol_handler(
    *,
    settings_path: str | Path = "config/settings.yaml",
    bm25_path: str | Path = "data/db/bm25",
    chunks_path: str | Path = "data/corpus/processed/chunks",
    image_roots: list[str | Path] | None = None,
) -> ProtocolHandler:
    """Wire configured retrieval services into MCP tools."""
    settings = load_settings(settings_path)
    vector_store = VectorStoreFactory.create(settings)
    bm25_indexer = BM25Indexer(bm25_path)
    if vector_store.count() == 0 or bm25_indexer.count() == 0:
        raise RuntimeError("No retrieval index found; run scripts/ingest.py first")

    hybrid_search = HybridSearch(
        settings,
        QueryProcessor(),
        DenseRetriever(EmbeddingFactory.create(settings), vector_store),
        SparseRetriever(bm25_indexer, vector_store),
        ReciprocalRankFusion(settings.retrieval.rrf_k),
    )
    catalog = CorpusCatalog(chunks_path)
    assembler = MultimodalAssembler(
        image_roots
        or [
            Path("data/images"),
            Path("data/corpus/processed/images"),
        ]
    )
    query_tool = QueryKnowledgeHubTool(
        hybrid_search,
        SafeReranker(RerankerFactory.create(settings)),
        ResponseBuilder(),
        assembler,
        TraceCollector(
            settings.observability.trace_file,
            enabled=settings.observability.enabled,
        ),
    )
    registry = ToolRegistry(
        [
            query_tool.definition(),
            ListCollectionsTool(catalog).definition(),
            GetDocumentSummaryTool(catalog).definition(),
        ]
    )
    return ProtocolHandler(registry)
