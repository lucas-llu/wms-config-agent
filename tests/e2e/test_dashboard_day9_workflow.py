from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

from core.query_engine import (
    DenseRetriever,
    HybridSearch,
    QueryProcessor,
    ReciprocalRankFusion,
    SafeReranker,
    SparseRetriever,
)
from core.settings import RetrievalSettings, SplitterSettings
from core.trace import TraceCollector
from core.types import Document
from ingestion import CorpusProcessor, DocumentManager, IndexingPipeline, IngestionPipeline
from ingestion.storage import BM25Indexer, LocalArtifactStorage
from ingestion.transform import BaseTransform
from libs.embedding import LocalLSAEmbedding
from libs.loader import BaseLoader
from libs.reranker import NoneReranker
from libs.vector_store import ChromaStore
from observability.dashboard.services import DataService, IngestionService, TraceService

pytestmark = pytest.mark.e2e


class _FixtureLoader(BaseLoader):
    def load(self, path, metadata=None) -> Document:
        source = Path(path)
        values = dict(metadata or {})
        values.update(
            {
                "source_path": source.as_posix(),
                "file_hash": self.compute_file_hash(source),
                "images": [],
                "title": "Sanitized Day 9 fixture",
            }
        )
        text = "\n\n".join(
            [
                "Directed putaway configuration selects storage locations and warehouse zones.",
                "Inbound appointment configuration controls dock capacity and receiving schedules.",
                "MOCA policy settings define safe warehouse execution behavior.",
                "Outbound staging configuration assigns shipping lanes and doors.",
            ]
        )
        return Document(self.build_document_id(values["file_hash"]), text, values)


class _IdentityTransform(BaseTransform):
    name = "identity"

    def transform(self, chunks, trace=None):
        del trace
        return [self.clone_chunk(chunk) for chunk in chunks]


class _NoImages:
    @staticmethod
    def list_images(*, collection=None, doc_hash=None):
        del collection, doc_hash
        return []

    @staticmethod
    def remove_document(doc_hash, *, collection=None):
        del doc_hash, collection
        return 0


def _pdf_payload() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_sanitized_dashboard_upload_inspect_trace_query_and_delete(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    processed = tmp_path / "processed"
    history = tmp_path / "db" / "history.db"
    trace_path = tmp_path / "logs" / "traces.jsonl"
    embedding = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models")
    vector_store = ChromaStore(
        persist_path=tmp_path / "chroma",
        collection_name="day9_chunks",
    )
    bm25 = BM25Indexer(tmp_path / "bm25")
    processor = CorpusProcessor(
        source_root=staging,
        output_root=processed,
        database_path=history,
        splitter_settings=SplitterSettings("recursive", 80, 10),
        transforms=(_IdentityTransform(),),
        loader_builder=lambda *args, **kwargs: _FixtureLoader(),
    )
    pipeline = IngestionPipeline(
        corpus_processor=processor,
        indexing_pipeline=IndexingPipeline(
            embedding=embedding,
            vector_store=vector_store,
            bm25_indexer=bm25,
            batch_size=2,
        ),
        trace_collector=TraceCollector(trace_path),
    )
    manager = DocumentManager(
        vector_store,
        bm25,
        _NoImages(),  # type: ignore[arg-type]
        processor.integrity,
        LocalArtifactStorage([staging, processed]),
        lifecycle_lock=pipeline.lifecycle_lock,
    )
    ingestion = IngestionService(pipeline, manager, staging_root=staging)

    ingestion_result = ingestion.ingest_pdf(
        "SWL.I.99.09 Sanitized Dashboard Fixture.pdf",
        _pdf_payload(),
        "day9-fixture",
    )
    data = DataService(manager, image_root=tmp_path / "images")
    documents = data.list_documents("day9-fixture")
    detail = data.get_document_detail(documents[0].doc_id)

    retrieval = RetrievalSettings("bm25", "rrf", 5, 5, 3, 60, 2, 0.0)
    query_trace = TraceCollector(trace_path)
    trace = query_trace.start(
        "query",
        {"query": "directed putaway storage location", "collection": "day9-fixture"},
    )
    outcome = HybridSearch(
        retrieval,
        QueryProcessor(),
        DenseRetriever(embedding, vector_store),
        SparseRetriever(bm25, vector_store),
        ReciprocalRankFusion(60),
    ).search_with_details(
        "directed putaway storage location",
        filters={"collection": "day9-fixture"},
        trace=trace,
    )
    reranked = SafeReranker(NoneReranker()).rerank(
        outcome.processed_query.retrieval_query,
        list(outcome.results),
        trace=trace,
    )
    assert trace is not None
    trace.finish()
    query_trace.collect(trace)

    traces = TraceService(trace_path)
    ingestion_traces = traces.list_traces("ingestion")
    query_traces = traces.list_traces("query", search="putaway")

    assert len(documents) == 1
    assert len(data.chunk_rows(detail)) == ingestion_result.indexing.vector_count
    assert reranked.results[0].metadata["collection"] == "day9-fixture"
    assert ingestion_traces.records[0].trace_id == ingestion_result.trace_id
    assert query_traces.records[0].trace_id == trace.trace_id
    assert traces.query_diagnostics(query_traces.records[0])["final_count"] > 0

    phrase = ingestion.deletion_phrase(documents[0])
    deletion = ingestion.delete_document(documents[0].doc_id, confirmation=phrase)

    assert deletion.success is True
    assert deletion.dense_deleted == deletion.sparse_deleted
    assert deletion.artifacts_deleted >= 3
    assert data.list_documents("day9-fixture") == []
    assert vector_store.count() == bm25.count() == 0
    assert not Path(ingestion_result.staged_pdf_path).exists()
