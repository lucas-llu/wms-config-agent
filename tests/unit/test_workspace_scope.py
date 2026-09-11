from __future__ import annotations

import sqlite3

import pytest

from agents.repositories import SessionNotFoundError, SessionRepository
from agents.workspace import Workspace, WorkspaceScopeError, WorkspaceService


def setup_workspaces(path):
    legacy = SessionRepository(path)
    for key in ("a", "b"):
        WorkspaceService(path).create(
            Workspace(
                f"workspace:{key}",
                key,
                (key,),
                ("inbound",),
                (key,),
                ("test",),
            )
        )
    return (
        legacy,
        SessionRepository(path, workspace_id="workspace:a"),
        SessionRepository(path, workspace_id="workspace:b"),
    )


def test_workspace_session_access_and_revisions_are_isolated(tmp_path):
    path = tmp_path / "sessions.db"
    legacy, a, b = setup_workspaces(path)
    a.create_session(session_id="session:a", goal="scope a")
    b.create_session(session_id="session:b", goal="scope b")
    assert [s.session_id for s in a.list_sessions()] == ["session:a"]
    assert legacy.list_sessions() == ()
    for operation in (
        lambda: a.get_session("session:b"),
        lambda: a.get_revision("session:b", 1),
        lambda: a.list_turns("session:b"),
        lambda: a.list_approvals("session:b"),
        lambda: a.list_exports("session:b"),
        lambda: a.update_revision(
            session_id="session:b",
            expected_revision=1,
            state_update={},
            actor="test",
            reason="cross-scope",
        ),
    ):
        with pytest.raises(SessionNotFoundError):
            operation()
    with pytest.raises(WorkspaceScopeError):
        a.update_revision(
            session_id="session:a",
            expected_revision=1,
            state_update={"workspace_id": "workspace:b"},
            actor="test",
            reason="move",
        )
    with pytest.raises(WorkspaceScopeError):
        a.update_revision(
            session_id="session:a",
            expected_revision=1,
            state_update={"confirmed_context": {"site": "b"}},
            actor="test",
            reason="escape",
        )
    assert a.get_session("session:a").current_revision == 1
    assert (
        SessionRepository(path, workspace_id="workspace:a")
        .get_revision("session:a")
        .state["workspace_id"]
        == "workspace:a"
    )


def test_scope_filters_require_selection_and_reject_missing_metadata(tmp_path):
    _, a, _ = setup_workspaces(tmp_path / "sessions.db")
    assert a.workspace.filters({"version": "2024.1"}) == {
        "version": "2024.1",
        "collection": "a",
        "module": "inbound",
        "site": "a",
        "environment": "test",
    }
    with pytest.raises(WorkspaceScopeError):
        a.workspace.filters({"site": "b"})
    assert not a.workspace.permits_metadata({"collection": "a"})
    broad = Workspace("workspace:multi", "multi", ("a", "b"), ("inbound",), ("a",), ("test",))
    with pytest.raises(WorkspaceScopeError, match="Select"):
        broad.filters({})
    with pytest.raises(WorkspaceScopeError):
        Workspace("workspace:bad", "bad", (), ("inbound",), ("a",), ("test",))


def test_v1_migration_preserves_old_revision_and_is_repeatable(tmp_path):
    path = tmp_path / "sessions.db"
    repo = SessionRepository(path)
    repo.create_session(session_id="session:old", goal="old")
    original = repo.get_revision("session:old")
    # Recreate the v1 sessions table shape while preserving revision history.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER session_workspace_exists")
        connection.execute("DROP TRIGGER session_workspace_immutable")
        connection.execute("DROP INDEX idx_sessions_workspace")
        connection.execute("ALTER TABLE sessions DROP COLUMN workspace_id")
        connection.execute("PRAGMA user_version=1")
    for _ in range(2):
        migrated = SessionRepository(path)
        assert migrated.get_session("session:old").workspace_id == "workspace:legacy"
        assert migrated.get_revision("session:old") == original
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE sessions SET workspace_id='workspace:other'")
