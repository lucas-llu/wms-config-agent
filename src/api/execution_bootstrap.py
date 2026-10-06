"""Single-process shared resource factory; use a fixed bounded number of RQ workers."""

import hashlib
import os
from dataclasses import replace

from agents.tools import KnowledgeAdapter
from api.execution import create_execution_app
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
from ingestion.storage import BM25Indexer
from libs.embedding import EmbeddingFactory
from libs.reranker import RerankerFactory
from libs.vector_store import VectorStoreFactory
from multiuser.access import AccessStore
from multiuser.agent import UserAgent
from multiuser.control import RunControl
from multiuser.executor import RunExecutor
from multiuser.governor import ModelGovernor, ModelLimits
from multiuser.pooled_llm import PooledLLM
from multiuser.runs import RunLimits
from multiuser.session_authority import SessionAuthority
from workers.runs import redis_client


def execution_settings():
    def integer(name, default):
        return int(os.getenv("WMS_" + name, str(default)))

    runs = RunLimits(
        pending_per_user=integer("PENDING_PER_USER", 5),
        running_per_user=integer("RUNNING_PER_USER", 2),
        lease_seconds=integer("RUN_LEASE_SECONDS", 45),
        queue_seconds=integer("QUEUE_SECONDS", 600),
        execution_seconds=integer("EXECUTION_SECONDS", 600),
    )
    models = ModelLimits(
        inflight=integer("MODEL_INFLIGHT", 4),
        review_inflight=integer("REVIEW_INFLIGHT", 1),
        rpm=integer("MODEL_RPM", 60),
        tpm=integer("MODEL_TPM", 200000),
        wait_seconds=integer("MODEL_WAIT_SECONDS", 60),
        hold_seconds=integer("MODEL_HOLD_SECONDS", 900),
    )
    return runs, models, integer("EXECUTORS", 4)


def from_environment():
    settings = load_settings(os.getenv("WMS_SETTINGS_PATH", "config/settings.yaml"))
    runs, models, capacity = execution_settings()
    # There is exactly one provider retry layer, in GovernedLLM.
    if settings.llm.provider != "openai_compatible":
        raise ValueError(
            "Shared executor currently requires the bounded OpenAI-compatible transport"
        )
    llm = PooledLLM(replace(settings.llm, max_retries=0))
    if models.hold_seconds < settings.llm.timeout_seconds + 60:
        raise ValueError("Model hold must exceed provider timeout plus uncertainty guard")
    store = AccessStore(os.environ["WMS_USER_DB_DSN"], max_connections=8)
    control = RunControl(os.environ["WMS_CONTROL_DB_DSN"], limits=runs)
    authority = SessionAuthority(
        os.environ["WMS_OIDC_ISSUER"],
        os.environ["WMS_SESSION_CLIENT_ID"],
        os.environ["WMS_SESSION_CLIENT_SECRET"],
    )
    vector = VectorStoreFactory.create(settings)
    sparse = BM25Indexer("data/db/bm25")
    if vector.count() == 0 or sparse.count() == 0:
        raise RuntimeError("Read-only retrieval indexes are required")
    knowledge = KnowledgeAdapter(
        HybridSearch(
            settings,
            QueryProcessor(),
            DenseRetriever(EmbeddingFactory.create(settings), vector),
            SparseRetriever(sparse, vector),
            ReciprocalRankFusion(settings.retrieval.rrf_k),
        ),
        SafeReranker(RerankerFactory.create(settings)),
        ResponseBuilder(),
    )
    # Hash settings identity, never embed endpoint credentials in a key or metric.
    provider_key = hashlib.sha256(
        f"{settings.llm.base_url}\0{settings.llm.model}".encode()
    ).hexdigest()
    governor = ModelGovernor(control, provider_key, limits=models)
    retrieval = ModelGovernor(
        control,
        "wms-retrieval",
        limits=ModelLimits(
            inflight=capacity,
            review_inflight=0,
            rpm=100000,
            tpm=1000000,
            hold_seconds=models.hold_seconds,
            retries=0,
        ),
    )
    executor = RunExecutor(
        store,
        control,
        authority,
        governor,
        UserAgent(store, os.environ["WMS_USER_DB_DSN"], llm, settings.agent, knowledge),
        capacity=capacity,
        retrieval_governor=retrieval,
    )
    return create_execution_app(executor, redis_client(), os.environ["WMS_EXECUTION_TOKEN"])
