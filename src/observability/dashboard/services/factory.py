"""Construction of Dashboard services from the project configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ingestion import DocumentManager, create_ingestion_pipeline
from ingestion.storage import BM25Indexer, ImageStorage, LocalArtifactStorage
from libs.loader import SQLiteIntegrityChecker
from libs.vector_store import ChromaStore
from observability.dashboard.services.config_service import ConfigService
from observability.dashboard.services.data_service import DataService
from observability.dashboard.services.ingestion_service import IngestionService
from observability.dashboard.services.trace_service import TraceService


@dataclass(frozen=True, slots=True)
class DashboardServices:
    config: ConfigService
    data: DataService
    traces: TraceService


@lru_cache(maxsize=4)
def get_dashboard_services(settings_path: str | None = None) -> DashboardServices:
    path = settings_path or os.getenv("WMS_CONFIG_PATH", "config/settings.yaml")
    config = ConfigService.from_path(path)
    settings = config.settings
    chroma = ChromaStore(
        persist_path=settings.vector_store.persist_path,
        collection_name=settings.vector_store.collection_name,
        read_only=True,
    )
    bm25 = BM25Indexer(os.getenv("WMS_BM25_PATH", "data/db/bm25"), read_only=True)
    images = ImageStorage(
        settings.ingestion.image_storage.root_path,
        settings.ingestion.image_storage.database_path,
        read_only=True,
    )
    integrity = SQLiteIntegrityChecker(
        Path(os.getenv("WMS_INGESTION_HISTORY_PATH", "data/db/ingestion_history.db")),
        read_only=True,
    )
    manager = DocumentManager(chroma, bm25, images, integrity)
    return DashboardServices(
        config=config,
        data=DataService(manager, image_root=settings.ingestion.image_storage.root_path),
        traces=TraceService(settings.observability.trace_file),
    )


@lru_cache(maxsize=4)
def get_ingestion_service(settings_path: str | None = None) -> IngestionService:
    """Build write-capable services only when the management page explicitly requests them."""

    path = settings_path or os.getenv("WMS_CONFIG_PATH", "config/settings.yaml")
    config = ConfigService.from_path(path)
    settings = config.settings
    staging_root = Path(os.getenv("WMS_STAGING_PATH", "data/staging"))
    output_root = Path(os.getenv("WMS_PROCESSED_PATH", "data/corpus/processed"))
    history_path = Path(os.getenv("WMS_INGESTION_HISTORY_PATH", "data/db/ingestion_history.db"))
    bm25_path = Path(os.getenv("WMS_BM25_PATH", "data/db/bm25"))
    pipeline = create_ingestion_pipeline(
        settings,
        source_root=staging_root,
        output_root=output_root,
        history_path=history_path,
        bm25_path=bm25_path,
    )
    image_storage = pipeline.corpus_processor.image_storage or _DisabledImageStorage()
    manager = DocumentManager(
        pipeline.indexing_pipeline.vector_store,
        pipeline.indexing_pipeline.bm25_indexer,
        image_storage,
        pipeline.corpus_processor.integrity,
        LocalArtifactStorage([staging_root, output_root]),
        lifecycle_lock=pipeline.lifecycle_lock,
    )
    max_upload_mb = int(os.getenv("WMS_DASHBOARD_MAX_UPLOAD_MB", "25"))
    return IngestionService(
        pipeline,
        manager,
        staging_root=staging_root,
        max_upload_bytes=max_upload_mb * 1024 * 1024,
    )


class _DisabledImageStorage:
    @staticmethod
    def list_images(*, collection: str | None = None, doc_hash: str | None = None) -> list:
        del collection, doc_hash
        return []

    @staticmethod
    def remove_document(doc_hash: str, *, collection: str | None = None) -> int:
        del doc_hash, collection
        return 0
