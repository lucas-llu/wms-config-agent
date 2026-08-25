"""Composition root for the reusable interactive ingestion pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.settings import Settings
from core.trace import TraceCollector
from ingestion.corpus_processor import CorpusProcessor
from ingestion.pipeline import IndexingPipeline, IngestionPipeline
from ingestion.storage import BM25Indexer, ImageStorage
from ingestion.transform import ChunkRefiner, ImageCaptioner, MetadataEnricher
from libs.embedding import EmbeddingFactory
from libs.llm import LLMFactory
from libs.vector_store import VectorStoreFactory


def create_ingestion_pipeline(
    settings: Settings,
    *,
    source_root: str | Path = "data/staging",
    output_root: str | Path = "data/corpus/processed",
    history_path: str | Path = "data/db/ingestion_history.db",
    bm25_path: str | Path = "data/db/bm25",
) -> IngestionPipeline:
    """Build the same ingestion stack for CLI, Dashboard, and integration tests."""
    image_storage = None
    if settings.ingestion.image_storage.enabled:
        try:
            image_storage = ImageStorage(
                settings.ingestion.image_storage.root_path,
                settings.ingestion.image_storage.database_path,
            )
        except (OSError, sqlite3.Error):
            image_storage = None

    llm = LLMFactory.create(settings)
    vision_llm = LLMFactory.create_vision_llm(settings)
    processor = CorpusProcessor(
        source_root=source_root,
        output_root=output_root,
        database_path=history_path,
        splitter_settings=settings.splitter,
        extract_images=settings.ingestion.extract_images,
        transforms=(
            ChunkRefiner(settings, llm=llm),
            MetadataEnricher(settings, llm=llm),
            ImageCaptioner(settings, vision_llm=vision_llm),
        ),
        image_storage=image_storage,
        image_collection=settings.ingestion.image_storage.collection,
    )
    indexing = IndexingPipeline(
        embedding=EmbeddingFactory.create(settings),
        vector_store=VectorStoreFactory.create(settings),
        bm25_indexer=BM25Indexer(bm25_path),
        batch_size=settings.embedding.batch_size,
    )
    return IngestionPipeline(
        corpus_processor=processor,
        indexing_pipeline=indexing,
        trace_collector=TraceCollector(
            settings.observability.trace_file,
            enabled=settings.observability.enabled,
        ),
    )
