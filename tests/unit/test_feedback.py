from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.repositories import SessionNotFoundError, SessionRepository
from agents.repositories.feedback_repository import FEEDBACK_KINDS, FeedbackRepository
from agents.supervisor import RequirementSessionRunner
from agents.workspace import Workspace, WorkspaceService
from mcp_server.tool_registry import ToolRegistry
from mcp_server.tools.feedback import FeedbackTools


def setup(tmp_path):
    sessions = SessionRepository(tmp_path / "sessions.db")
    sessions.create_session(
        session_id="session:a", goal="private goal", initial_state={"trace_id": "a" * 32}
    )
    return sessions, FeedbackRepository(sessions)


@pytest.mark.parametrize("kind", FEEDBACK_KINDS)
def test_feedback_durable_deduplicated_and_revision_immutable(tmp_path, kind):
    sessions, feedback = setup(tmp_path)
    before = sessions.get_revision("session:a", 1)
    reason = "unclear_answer" if kind == "regeneration" else ""
    first = feedback.record("session:a", 1, kind, reason)
    assert first == FeedbackRepository(sessions).record("session:a", 1, kind, reason)
    assert first["workspace_id"] == "workspace:legacy"
    assert first["trace_id"] == "a" * 32
    assert feedback.summary("session:a", 1)["total"] == 1
    assert sessions.get_revision("session:a", 1) == before
    assert sessions.get_session("session:a").current_revision == 1
    assert "private goal" not in str(first)


def test_feedback_scope_and_historical_revision(tmp_path):
    sessions, feedback = setup(tmp_path)
    sessions.update_revision(
        session_id="session:a",
        expected_revision=1,
        state_update={"trace_id": "b" * 32},
        actor="system",
        reason="turn",
    )
    assert feedback.record("session:a", 1, "thumbs_up")["trace_id"] == "a" * 32
    assert feedback.record("session:a", 2, "thumbs_down")["trace_id"] == "b" * 32
    WorkspaceService(sessions.database_path).create(
        Workspace("workspace:other", "other", ("c",), ("m",), ("s",), ("e",))
    )
    other = FeedbackRepository(
        SessionRepository(sessions.database_path, workspace_id="workspace:other")
    )
    for action in (
        lambda: other.record("session:a", 1, "thumbs_up"),
        lambda: other.summary("session:a", 1),
        lambda: feedback.record("session:a", 99, "thumbs_up"),
    ):
        with pytest.raises(SessionNotFoundError):
            action()


@pytest.mark.parametrize("trace", ["", "secret-value", None])
def test_unavailable_trace_is_not_fabricated_or_copied(tmp_path, trace):
    sessions, feedback = setup(tmp_path)
    sessions.create_session(
        session_id="no-trace", goal="private", initial_state={"trace_id": trace}
    )
    assert feedback.record("no-trace", 1, "thumbs_up")["trace_id"] is None


@pytest.mark.parametrize(
    "extra",
    [
        {"comment": "secret"},
        {"trace_id": "secret"},
        {"workspace_id": "secret"},
        {"revision": True},
        {"revision": 0},
        {"kind": "secret"},
        {"reason": "secret"},
        {"kind": "regeneration"},
    ],
)
def test_invalid_feedback_never_persisted(tmp_path, extra):
    sessions, feedback = setup(tmp_path)
    registry = ToolRegistry(FeedbackTools(feedback).definitions())
    response = registry.call(
        "record_configuration_feedback",
        {"session_id": "session:a", "revision": 1, "kind": "thumbs_up", **extra},
    )
    assert response["isError"]
    assert feedback.summary("session:a", 1)["total"] == 0
    with sqlite3.connect(sessions.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM feedback_signals").fetchone()[0] == 0


def test_tool_annotations_and_summary(tmp_path):
    _, feedback = setup(tmp_path)
    registry = ToolRegistry(FeedbackTools(feedback).definitions())
    record = registry.call(
        "record_configuration_feedback",
        {"session_id": "session:a", "revision": 1, "kind": "citation_error"},
    )
    assert not record["isError"]
    result = registry.call(
        "get_configuration_feedback_summary", {"session_id": "session:a", "revision": 1}
    )
    assert result["structuredContent"]["total"] == 1
    tools = {tool["name"]: tool for tool in registry.definitions()}
    assert not tools["record_configuration_feedback"]["annotations"]["readOnlyHint"]
    assert tools["get_configuration_feedback_summary"]["annotations"]["readOnlyHint"]


@pytest.mark.parametrize("with_trace", [True, False])
def test_runner_persists_current_trace_not_checkpoint_trace(with_trace):
    sessions = Mock()
    sessions.update_revision.return_value = SimpleNamespace(revision=2)
    runner = RequirementSessionRunner(supervisor=Mock(), sessions=sessions)
    graph = Mock()
    graph.aget_state = AsyncMock(
        return_value=SimpleNamespace(
            values={"session_id": "session:a", "trace_id": "stale", "status": "created"},
            interrupts=(),
            next=(),
        )
    )
    trace = Mock(trace_id="a" * 32) if with_trace else None
    asyncio.run(runner._persist_result(graph, {}, 1, trace))
    assert sessions.update_revision.call_args.kwargs["state_update"]["trace_id"] == (
        "a" * 32 if with_trace else ""
    )
