"""Construction of Dashboard services from the project configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ingestion import DocumentManager
from ingestion.storage import BM25Indexer, ImageStorage
from libs.loader import SQLiteIntegrityChecker
from libs.vector_store import ChromaStore
from observability.dashboard.services.config_service import ConfigService
from observability.dashboard.services.data_service import DataService


@dataclass(frozen=True, slots=True)
class DashboardServices:
    config: ConfigService
    data: DataService


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
    )
