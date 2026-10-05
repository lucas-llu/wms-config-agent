import asyncio
import sqlite3

import pytest

from agents.repositories import SessionNotFoundError, SessionRepository, SessionRevisionConflict
from agents.repositories.feedback_repository import FeedbackRepository
from agents.runtime import (
    open_async_sqlite_checkpointer,
    run_runtime_probe,
    session_checkpoint_config,
)
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
    with pytest.raises(SessionNotFoundError):
        other.rename_session("session:a", "Wrong workspace")
    repo.delete_session("session:a")
    assert other.list_deleted_sessions() == ()
    with pytest.raises(SessionNotFoundError):
        other.restore_session("session:a")
    repo.restore_session("session:a")
    assert repo.list_sessions()


def test_rename_persists_as_display_metadata_without_changing_goal(tmp_path):
    path = tmp_path / "sessions.db"
    repo = SessionRepository(path)
    repo.create_session(session_id="session:a", goal="Original configuration requirement")
    revision = repo.get_revision("session:a")
    repo.rename_session("session:a", "Display title")
    restarted = SessionRepository(path)
    assert restarted.get_session("session:a").goal == "Original configuration requirement"
    assert restarted.get_session("session:a").display_title == "Display title"
    assert restarted.get_revision("session:a") == revision
    restarted.delete_session("session:a")
    assert restarted.list_deleted_sessions()[0].display_title == "Display title"
    restarted.restore_session("session:a")
    assert restarted.get_session("session:a").display_title == "Display title"


@pytest.mark.parametrize("title", ["", "  ", "x" * 121, "line\nbreak"])
def test_invalid_title_cannot_change_history(tmp_path, title):
    repo = SessionRepository(tmp_path / "sessions.db")
    repo.create_session(session_id="session:a", goal="Original")
    with pytest.raises(ValueError):
        repo.rename_session("session:a", title)
    assert repo.get_session("session:a").display_title is None


def _trash(repo, *ids):
    for sid in ids:
        repo.create_session(session_id=sid, goal=sid)
        repo.append_turn(session_id=sid, expected_revision=1, role="user", message="test question")
        repo.rename_session(sid, "display " + sid)
        repo.delete_session(sid)


def test_purge_removes_selected_related_records_and_checkpoints(tmp_path):
    repo = SessionRepository(tmp_path / "sessions.db")
    _trash(repo, "a", "b", "keep")
    repo.restore_session("a")
    FeedbackRepository(repo).record("a", 1, "thumbs_up")
    repo.delete_session("a")
    checkpoint = tmp_path / "checkpoints.db"
    with sqlite3.connect(checkpoint) as db:
        db.executescript(
            "CREATE TABLE checkpoints(thread_id TEXT); CREATE TABLE writes(thread_id TEXT);"
        )
        for table in ("checkpoints", "writes"):
            db.executemany(f"INSERT INTO {table} VALUES (?)", [("a",), ("b",), ("keep",)])
    assert repo.purge_deleted_sessions({"a": 1, "b": 1}, checkpoint_path=checkpoint) == 2
    restarted = SessionRepository(repo.database_path)
    assert [s.session_id for s in restarted.list_deleted_sessions()] == ["keep"]
    for sid in ("a", "b"):
        with pytest.raises(SessionNotFoundError):
            restarted.restore_session(sid)
    with sqlite3.connect(repo.database_path) as db:
        for table in (
            "sessions",
            "turns",
            "revisions",
            "conversation_titles",
            "deleted_sessions",
            "feedback_signals",
        ):
            assert (
                db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id IN ('a','b')"
                ).fetchone()[0]
                == 0
            )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(checkpoint) as db:
        for table in ("checkpoints", "writes"):
            assert db.execute(f"SELECT thread_id FROM {table}").fetchall() == [("keep",)]


@pytest.mark.parametrize("changed", ["restored", "revision", "foreign", "missing"])
def test_purge_rechecks_entire_batch_before_deleting(tmp_path, changed):
    repo = SessionRepository(tmp_path / "sessions.db")
    _trash(repo, "a", "b")
    target = "b"
    if changed in {"restored", "revision"}:
        repo.restore_session("b")
        if changed == "revision":
            repo.update_revision(
                session_id="b", expected_revision=1, state_update={}, actor="test", reason="change"
            )
            repo.delete_session("b")
    elif changed == "foreign":
        WorkspaceService(repo.database_path).create(
            Workspace("workspace:other", "Other", ("c",), ("m",), ("s",), ("e",))
        )
        other = SessionRepository(repo.database_path, workspace_id="workspace:other")
        _trash(other, "foreign")
        target = "foreign"
    else:
        target = "missing"
    with pytest.raises((ValueError, SessionNotFoundError, SessionRevisionConflict)):
        repo.purge_deleted_sessions({"a": 1, target: 1})
    assert "a" in {s.session_id for s in repo.list_deleted_sessions()}
    repo.restore_session("a")
    assert repo.list_turns("a")


def test_purge_checkpoint_failure_rolls_back_records(tmp_path):
    repo = SessionRepository(tmp_path / "sessions.db")
    _trash(repo, "a")
    checkpoint = tmp_path / "broken.db"
    with sqlite3.connect(checkpoint) as db:
        db.executescript(
            "CREATE TABLE checkpoints(thread_id TEXT); CREATE TABLE writes(wrong TEXT);"
        )
    with pytest.raises(sqlite3.OperationalError):
        repo.purge_deleted_sessions({"a": 1}, checkpoint_path=checkpoint)
    repo.restore_session("a")
    assert repo.list_turns("a")


def test_purge_is_not_limited_to_first_100_and_preserves_new_trash(tmp_path):
    repo = SessionRepository(tmp_path / "sessions.db")
    _trash(repo, *(f"old-{i}" for i in range(103)))
    snapshot = {s.session_id: s.current_revision for s in repo.list_deleted_sessions(limit=None)}
    _trash(repo, "new-arrival")
    assert repo.purge_deleted_sessions(snapshot) == 103
    assert [s.session_id for s in repo.list_deleted_sessions()] == ["new-arrival"]


@pytest.mark.parametrize("selection", [{}, {"a": True}, {"a": 0}, {"a": "1"}, {"a": 1, " a ": 1}])
def test_invalid_purge_selection_leaves_records_intact(tmp_path, selection):
    repo = SessionRepository(tmp_path / "sessions.db")
    _trash(repo, "a")
    with pytest.raises(ValueError):
        repo.purge_deleted_sessions(selection)
    assert len(repo.list_deleted_sessions()) == 1


def test_purge_cleans_real_langgraph_checkpoints(tmp_path):
    checkpoint = tmp_path / "graph.db"
    assert asyncio.run(run_runtime_probe(checkpoint))["completed"]
    repo = SessionRepository(tmp_path / "sessions.db")
    _trash(repo, "day1-runtime-probe")
    repo.purge_deleted_sessions({"day1-runtime-probe": 1}, checkpoint_path=checkpoint)

    async def read_checkpoint():
        async with open_async_sqlite_checkpointer(checkpoint) as saver:
            return await saver.aget_tuple(session_checkpoint_config("day1-runtime-probe"))

    assert asyncio.run(read_checkpoint()) is None


def test_business_delete_failure_rolls_back_checkpoint_cleanup(tmp_path):
    checkpoint = tmp_path / "graph.db"
    asyncio.run(run_runtime_probe(checkpoint))
    repo = SessionRepository(tmp_path / "sessions.db")
    _trash(repo, "day1-runtime-probe")
    with sqlite3.connect(repo.database_path) as db:
        db.executescript(
            "CREATE TRIGGER reject_purge BEFORE DELETE ON sessions "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END;"
        )
    with pytest.raises(sqlite3.IntegrityError):
        repo.purge_deleted_sessions({"day1-runtime-probe": 1}, checkpoint_path=checkpoint)
    assert len(repo.list_deleted_sessions()) == 1
    with sqlite3.connect(checkpoint) as db:
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] > 0
