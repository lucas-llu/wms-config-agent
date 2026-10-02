import sqlite3

import pytest

from agents.repositories import SessionNotFoundError, SessionRepository
from agents.workspace import Workspace, WorkspaceService


def test_delete_restore_is_durable_and_preserves_revision_and_turns(tmp_path):
    path = tmp_path / "sessions.db"
    repo = SessionRepository(path)
    repo.create_session(session_id="session:a", goal="test goal")
    repo.append_turn(session_id="session:a", expected_revision=1, role="user", message="question")
    before = repo.get_revision("session:a")
    turns = repo.list_turns("session:a")
    repo.delete_session("session:a")
    repo.delete_session("session:a")
    assert repo.list_sessions() == ()
    restarted = SessionRepository(path)
    assert restarted.list_deleted_sessions()[0].session_id == "session:a"
    for operation in (
        lambda: restarted.get_revision("session:a"),
        lambda: restarted.list_turns("session:a"),
        lambda: restarted.append_turn(
            session_id="session:a", expected_revision=1, role="assistant", message="late answer"
        ),
    ):
        with pytest.raises(SessionNotFoundError):
            operation()
    restarted.restore_session("session:a")
    assert restarted.get_revision("session:a") == before
    assert restarted.list_turns("session:a") == turns
    assert restarted.list_deleted_sessions() == ()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_other_workspace_cannot_delete_restore_or_list_history(tmp_path):
    path = tmp_path / "sessions.db"
    repo = SessionRepository(path)
    repo.create_session(session_id="session:a", goal="A")
    WorkspaceService(path).create(Workspace("workspace:b", "B", ("c",), ("m",), ("s",), ("e",)))
    other = SessionRepository(path, workspace_id="workspace:b")
    with pytest.raises(SessionNotFoundError):
        other.delete_session("session:a")
    repo.delete_session("session:a")
    assert other.list_deleted_sessions() == ()
    with pytest.raises(SessionNotFoundError):
        other.restore_session("session:a")
    repo.restore_session("session:a")
    assert repo.list_sessions()
