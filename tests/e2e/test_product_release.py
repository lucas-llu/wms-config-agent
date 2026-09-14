from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agents.repositories import SessionRepository
from agents.repositories.feedback_repository import FeedbackRepository
from agents.workspace import Workspace, WorkspaceService
from core.settings import load_settings
from mcp_server import app as mcp_app
from mcp_server.tool_registry import ToolRegistry
from mcp_server.tools.feedback import FeedbackTools
from observability.dashboard.services.workbench_service import WorkbenchService


@pytest.mark.parametrize("enabled", [False, True])
def test_live_app_discovery_and_feedback_boundary(tmp_path, monkeypatch, enabled):
    path = tmp_path / "sessions.db"
    SessionRepository(path)
    for name in ("a", "b"):
        WorkspaceService(path).create(
            Workspace(f"workspace:{name}", name, (name,), ("inbound",), (name,), ("test",))
        )
        SessionRepository(path, workspace_id=f"workspace:{name}").create_session(
            session_id=f"session:{name}", goal="private-canary"
        )
    settings = load_settings()
    settings = replace(
        settings,
        agent=replace(
            settings.agent, enabled=enabled, workspace_id="workspace:a", session_db_path=path
        ),
        observability=replace(settings.observability, enabled=False),
    )
    monkeypatch.setattr(mcp_app, "load_settings", lambda _: settings)
    for factory in (
        mcp_app.EmbeddingFactory,
        mcp_app.RerankerFactory,
        mcp_app.LLMFactory,
    ):
        monkeypatch.setattr(factory, "create", lambda _: object())

    class Index:
        def count(self):
            return 1

    monkeypatch.setattr(mcp_app.VectorStoreFactory, "create", lambda _: Index())
    monkeypatch.setattr(mcp_app, "BM25Indexer", lambda _: Index())
    registry = mcp_app.create_protocol_handler(chunks_path=tmp_path).registry
    definitions = registry.definitions()
    actions = registry.call("get_agent_actions", {})["structuredContent"]["actions"]
    assert [item["name"] for item in actions] == [item["name"] for item in definitions]
    assert "private-canary" not in json.dumps(actions)
    if enabled:
        capabilities = registry.call("get_agent_capabilities", {})["structuredContent"]
        assert capabilities["tools"] == [
            {key: item[key] for key in ("name", "title", "annotations")} for item in actions
        ]
        denied = registry.call(
            "record_configuration_feedback",
            {"session_id": "session:b", "revision": 1, "kind": "thumbs_down"},
        )
        assert denied["isError"]
        assert "private-canary" not in json.dumps(denied)
        accepted = registry.call(
            "record_configuration_feedback",
            {"session_id": "session:a", "revision": 1, "kind": "thumbs_up"},
        )
        assert not accepted["isError"]
    else:
        assert "record_configuration_feedback" not in [item["name"] for item in actions]


def test_legacy_migration_then_feedback_preserves_snapshot(tmp_path):
    path = tmp_path / "migration.db"
    repository = SessionRepository(path)
    repository.create_session(session_id="session:old", goal="private-canary")
    original = repository.get_revision("session:old")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER session_workspace_exists")
        connection.execute("DROP TRIGGER session_workspace_immutable")
        connection.execute("DROP INDEX idx_sessions_workspace")
        connection.execute("ALTER TABLE sessions DROP COLUMN workspace_id")
        connection.execute("PRAGMA user_version=1")
    for _ in range(2):
        migrated = SessionRepository(path)
        feedback = FeedbackRepository(migrated)
        feedback.record("session:old", 1, "incomplete_answer")
        assert migrated.get_revision("session:old") == original
        assert feedback.summary("session:old", 1)["total"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        rows = connection.execute("SELECT * FROM feedback_signals").fetchall()
        assert "private-canary" not in str(rows)


def test_workbench_feedback_concurrent_retry_and_restart(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.db")
    repository.create_session(
        session_id="session:a", goal="private-canary", initial_state={"trace_id": "a" * 32}
    )
    feedback = FeedbackRepository(repository)
    registry = ToolRegistry(FeedbackTools(feedback).definitions())
    workbench = WorkbenchService(repository, registry.call, enabled=True)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _: workbench.act(
                    "feedback", "session:a", 1, kind="regeneration", reason="unclear_answer"
                ),
                range(8),
            )
        )
    assert all(not result["isError"] for result in results)
    assert len({result["structuredContent"]["feedback_id"] for result in results}) == 1
    restarted = FeedbackRepository(SessionRepository(repository.database_path))
    assert restarted.summary("session:a", 1)["total"] == 1
    assert repository.get_session("session:a").current_revision == 1
    assert repository.list_approvals("session:a") == ()
