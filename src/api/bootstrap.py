"""Compose the authenticated ASGI service without legacy host-process tools."""

import os
from pathlib import Path

from agents.tools import KnowledgeAdapter
from api.users import create_app
from api.web import mount_workbench
from core.query_engine import (
    DenseRetriever,
    HybridSearch,
    QueryProcessor,
    ReciprocalRankFusion,
    SafeReranker,
    SparseRetriever,
)
from core.response import ResponseBuilder
from core.settings import load_settings
from ingestion.storage import BM25Indexer, ImageStorage
from libs.embedding import EmbeddingFactory
from libs.llm import LLMFactory
from libs.reranker import RerankerFactory
from libs.vector_store import VectorStoreFactory
from multiuser.access import AccessStore
from multiuser.account import AccountClient
from multiuser.agent import UserAgent
from multiuser.application import OwnedApplication
from multiuser.identity import OIDCVerifier
from multiuser.session_auth import TokenIntrospector
from observability.dashboard.services.evidence_images import EvidenceImages


def from_environment():
    """Requires operator-configured HTTPS issuer and a migrated non-owner PG role."""
    issuer = os.environ["WMS_OIDC_ISSUER"]
    dsn = os.environ["WMS_USER_DB_DSN"]
    verifier = OIDCVerifier(issuer, "wms-api")
    introspector = TokenIntrospector(
        issuer, os.getenv("WMS_API_CLIENT_ID", "wms-api"), os.environ["WMS_API_CLIENT_SECRET"]
    )
    settings = load_settings(os.getenv("WMS_SETTINGS_PATH", "config/settings.yaml"))
    store = AccessStore(dsn)
    try:
        from multiuser.usage_bootstrap import metering_from_environment

        store.usage = metering_from_environment(settings)
        if os.getenv("WMS_P3_ENABLED") == "1":
            return queued_api(verifier, introspector, store, issuer, settings)
        vector = VectorStoreFactory.create(settings)
        sparse = BM25Indexer("data/db/bm25")
        if vector.count() == 0 or sparse.count() == 0:
            raise RuntimeError("A retrieval index is required")
        search = HybridSearch(
            settings,
            QueryProcessor(),
            DenseRetriever(EmbeddingFactory.create(settings), vector),
            SparseRetriever(sparse, vector),
            ReciprocalRankFusion(settings.retrieval.rrf_k),
        )
        knowledge = KnowledgeAdapter(
            search, SafeReranker(RerankerFactory.create(settings)), ResponseBuilder()
        )
        agent = UserAgent(store, dsn, LLMFactory.create(settings), settings.agent, knowledge)
        images = EvidenceImages(
            ImageStorage(
                settings.ingestion.image_storage.root_path,
                settings.ingestion.image_storage.database_path,
                read_only=True,
            ),
            [settings.ingestion.image_storage.root_path],
        )
        application = OwnedApplication(
            store, export_root=settings.agent.export_root / "users", agent=agent, images=images
        )
        origins = tuple(o.strip() for o in os.getenv("WMS_WEB_ORIGINS", "").split(",") if o.strip())
        app = create_app(
            verifier,
            introspector,
            application,
            allowed_origins=origins,
            account=AccountClient(issuer),
            web_config={
                "issuer": issuer,
                "client_id": os.getenv("WMS_WEB_CLIENT_ID", "wms-workbench"),
            },
        )
        app.state.user_store = store
        if directory := os.getenv("WMS_FRONTEND_DIST"):
            mount_workbench(app, Path(directory), issuer)
        return app
    except Exception:
        store.close()
        verifier.close()
        introspector.close()
        raise


def queued_api(verifier, introspector, store, issuer, settings):
    from api.execution_bootstrap import execution_settings
    from workers.runs import ExecutionClient

    runs, _, _ = execution_settings()
    client = ExecutionClient(os.environ["WMS_EXECUTION_URL"], os.environ["WMS_EXECUTION_TOKEN"])
    try:
        if client.request("/internal/ready") != {"ready": True, "protocol": "wms-runs-v1"}:
            raise RuntimeError("Compatible shared execution service required")
    finally:
        client.close()
    # Public API never loads model/index/embedding objects; only the private executor does.
    images = EvidenceImages(
        ImageStorage(
            settings.ingestion.image_storage.root_path,
            settings.ingestion.image_storage.database_path,
            read_only=True,
        ),
        [settings.ingestion.image_storage.root_path],
    )
    application = OwnedApplication(
        store, export_root=settings.agent.export_root / "users", images=images
    )
    app = create_app(
        verifier,
        introspector,
        application,
        allowed_origins=tuple(
            o.strip() for o in os.getenv("WMS_WEB_ORIGINS", "").split(",") if o.strip()
        ),
        account=AccountClient(issuer),
        run_limits=runs,
        durable_execution=True,
        web_config={"issuer": issuer, "client_id": os.getenv("WMS_WEB_CLIENT_ID", "wms-workbench")},
    )
    app.state.user_store = store
    if directory := os.getenv("WMS_FRONTEND_DIST"):
        mount_workbench(app, Path(directory), issuer)
    return app
