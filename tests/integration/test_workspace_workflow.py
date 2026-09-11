from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from agents.repositories import SessionNotFoundError, SessionRepository
from agents.runtime import open_configured_checkpointer
from agents.services import SessionService, SolutionService
from agents.supervisor import RequirementSessionRunner, Supervisor
from agents.tools.workspace_search import WorkspaceSearch
from agents.workspace import Workspace, WorkspaceScopeError, WorkspaceService
from core.query_engine import QueryProcessor, SearchOutcome
from core.settings import load_settings
from core.types import Chunk, RetrievalResult
from libs.llm import ChatResponse
from mcp_server import app as mcp_app
from mcp_server.catalog import CorpusCatalog


def provision(path):
    SessionRepository(path)
    policy = Workspace("workspace:a", "A", ("a",), ("inbound",), ("DC01",), ("test",))
    WorkspaceService(path).create(policy)
    return policy, SessionRepository(path, workspace_id=policy.workspace_id)


def test_llm_cannot_expand_scope_or_trigger_planning(tmp_path):
    policy, repository = provision(tmp_path / "sessions.db")

    class LLM:
        calls = 0

        def chat(self, messages, trace=None):
            self.calls += 1
            return ChatResponse(
                json.dumps(
                    {
                        "confirmed_context": {
                            "site": "DC02",
                            "modules": ["inbound"],
                            "environment": "test",
                            "product_version": "2024.1",
                            "business_process": "receiving",
                        },
                        "assumptions": [],
                        "summary": "Out of scope",
                    }
                )
            )

    llm = LLM()
    settings = replace(load_settings().agent, checkpoint_path=tmp_path / "checkpoints.db")
    runner = RequirementSessionRunner(
        supervisor=Supervisor(llm=llm, settings=settings, workspace=policy),
        sessions=SessionService(repository),
    )

    async def run():
        async with open_configured_checkpointer(settings) as saver:
            return await runner.start("Build an inbound configuration plan", checkpointer=saver)

    result = asyncio.run(run())
    assert llm.calls == 1
    assert result.state["status"] == "paused"
    assert result.state["pause_reason"] == "workspace_scope_invalid"
    assert result.state["confirmed_context"] == {}
    assert result.state["configuration_tasks"] == []
    assert result.state["workspace_id"] == policy.workspace_id


def test_retrieval_scope_is_injected_and_untrusted_results_filtered(tmp_path):
    policy, _ = provision(tmp_path / "sessions.db")
    good_metadata = {
        "source_path": "good.pdf",
        "collection": "a",
        "module": "inbound",
        "site": "DC01",
        "environment": "test",
    }
    hits = (
        RetrievalResult("good", 0.9, "Allowed", good_metadata),
        RetrievalResult(
            "bad", 0.9, "Private other workspace", {**good_metadata, "collection": "b"}
        ),
    )

    class Backend:
        calls = []

        def search_with_details(self, query, top_k, filters, trace=None):
            self.calls.append(filters)
            return SearchOutcome(
                QueryProcessor().process(query, filters), hits, hits, hits, hits, {}, True
            )

    backend = Backend()
    search = WorkspaceSearch(backend, policy)
    outcome = search.search_with_details("receiving", 5, {})
    assert backend.calls[0]["collection"] == "a"
    assert backend.calls[0]["site"] == "DC01"
    assert [r.chunk_id for r in outcome.results] == ["good"]
    assert "Private" not in str(outcome)
    with pytest.raises(WorkspaceScopeError):
        search.search_with_details("receiving", 5, {"collection": "b"})
    assert len(backend.calls) == 1


def test_cross_workspace_approval_export_never_writes(tmp_path):
    _, a = provision(tmp_path / "sessions.db")
    legacy = SessionRepository(tmp_path / "sessions.db")
    legacy.create_session(session_id="session:legacy", goal="Legacy")
    exports = tmp_path / "exports"
    service = SolutionService(a, exports)
    with pytest.raises(SessionNotFoundError):
        service.export("session:legacy", expected_revision=1, format="json")
    assert not exports.exists()
    assert legacy.list_exports("session:legacy") == ()


def test_catalog_counts_and_summaries_exclude_other_workspace(tmp_path, monkeypatch):
    policy, _ = provision(tmp_path / "sessions.db")
    chunks = [
        Chunk(
            id=key,
            text=key,
            metadata={
                "source_path": f"{key}.pdf",
                "collection": key,
                "module": "inbound",
                "site": "DC01",
                "environment": "test",
            },
            start_offset=0,
            end_offset=1,
        )
        for key in ("a", "b")
    ]
    monkeypatch.setattr("mcp_server.catalog.load_preprocessed_chunks", lambda _: chunks)
    catalog = CorpusCatalog(tmp_path, workspace=policy)
    assert [c["name"] for c in catalog.list_collections()] == ["a"]
    assert catalog.find_documents("b.pdf") == []
    assert catalog.find_documents("a.pdf")[0].chunk_count == 1


def test_mcp_app_selects_one_workspace_for_all_session_tools(tmp_path, monkeypatch):
    policy, scoped = provision(tmp_path / "sessions.db")
    scoped.create_session(session_id="session:a", goal="A")
    legacy = SessionRepository(tmp_path / "sessions.db")
    legacy.create_session(session_id="session:legacy", goal="Legacy")
    settings = load_settings()
    settings = replace(
        settings,
        agent=replace(
            settings.agent,
            enabled=True,
            workspace_id=policy.workspace_id,
            session_db_path=tmp_path / "sessions.db",
            checkpoint_path=tmp_path / "checkpoints.db",
        ),
    )

    class Store:
        def count(self):
            return 1

    monkeypatch.setattr(mcp_app, "load_settings", lambda _: settings)
    monkeypatch.setattr(mcp_app.VectorStoreFactory, "create", lambda _: Store())
    monkeypatch.setattr(mcp_app, "BM25Indexer", lambda _: Store())
    monkeypatch.setattr(mcp_app.EmbeddingFactory, "create", lambda _: object())
    monkeypatch.setattr(mcp_app.RerankerFactory, "create", lambda _: object())
    monkeypatch.setattr(mcp_app.LLMFactory, "create", lambda _: object())
    settings = replace(settings, observability=replace(settings.observability, enabled=False))
    handler = mcp_app.create_protocol_handler()
    own = handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_configuration_session",
                "arguments": {"session_id": "session:a"},
            },
        }
    )
    assert own["result"]["structuredContent"]["state"]["workspace_id"] == policy.workspace_id
    other = handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_configuration_session",
                "arguments": {"session_id": "session:legacy"},
            },
        }
    )
    assert "state" not in str(other)
    assert "error" in other or other["result"]["isError"]
