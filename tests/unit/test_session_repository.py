from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from agents.contracts import (
    Decision,
    ExportArtifact,
    ReviewDecision,
    SessionStatus,
    state_fingerprint,
)
from agents.repositories import (
    SessionAlreadyExistsError,
    SessionRepository,
    SessionRepositoryError,
    SessionRevisionConflict,
)
from agents.services import SessionService


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 27, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def _repository(path: Path) -> SessionRepository:
    return SessionRepository(path, clock=AdvancingClock())


def test_schema_uses_wal_foreign_keys_and_all_required_tables(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "sessions.db")

    with repository._read_connection() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert {"sessions", "revisions", "turns", "decisions", "approvals", "exports"} <= tables
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert schema_version == 1


def test_two_sessions_remain_isolated_when_turns_are_interleaved(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "sessions.db")
    service = SessionService(repository)
    first = service.create_session("Configure receiving", session_id="session:first")
    second = service.create_session("Configure shipping", session_id="session:second")

    service.append_turn(
        first.session_id,
        expected_revision=1,
        role="user",
        message="Receiving requirements",
    )
    service.append_turn(
        second.session_id,
        expected_revision=1,
        role="user",
        message="Shipping requirements",
    )
    service.append_turn(
        first.session_id,
        expected_revision=1,
        role="assistant",
        message="Receiving follow-up",
    )

    assert [turn.message for turn in repository.list_turns(first.session_id)] == [
        "Receiving requirements",
        "Receiving follow-up",
    ]
    assert [turn.message for turn in repository.list_turns(second.session_id)] == [
        "Shipping requirements"
    ]
    assert first.checkpoint_thread_id == first.session_id
    assert second.checkpoint_thread_id == second.session_id


def test_stale_revision_is_rejected_across_repository_instances(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    first_writer = _repository(database)
    second_writer = _repository(database)
    first_writer.create_session(session_id="session:shared", goal="Configure inbound")

    revision = first_writer.update_revision(
        session_id="session:shared",
        expected_revision=1,
        state_update={"status": SessionStatus.PLANNING.value},
        actor="planning",
        reason="requirements_complete",
    )

    assert revision.revision == 2
    with pytest.raises(SessionRevisionConflict) as error:
        second_writer.update_revision(
            session_id="session:shared",
            expected_revision=1,
            state_update={"status": SessionStatus.RETRIEVING.value},
            actor="knowledge",
            reason="stale_write",
        )
    assert error.value.expected == 1
    assert error.value.actual == 2
    assert first_writer.get_session("session:shared").current_revision == 2
    assert first_writer.get_revision("session:shared", 1).status is SessionStatus.CREATED
    assert first_writer.get_revision("session:shared", 2).status is SessionStatus.PLANNING


def test_concurrent_revision_writers_allow_exactly_one_commit(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    _repository(database).create_session(session_id="session:concurrent", goal="Configure inbound")
    barrier = Barrier(2)

    def write(status: SessionStatus) -> str:
        repository = SessionRepository(database)
        barrier.wait(timeout=5)
        try:
            repository.update_revision(
                session_id="session:concurrent",
                expected_revision=1,
                state_update={"status": status.value},
                actor=status.value,
                reason="concurrent_test",
            )
            return "committed"
        except SessionRevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, (SessionStatus.PLANNING, SessionStatus.RETRIEVING)))

    recovered = SessionRepository(database)
    assert sorted(outcomes) == ["committed", "conflict"]
    assert recovered.get_session("session:concurrent").current_revision == 2
    assert [item.revision for item in recovered.list_revisions("session:concurrent")] == [1, 2]


def test_append_turn_rejects_stale_revision(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "sessions.db")
    repository.create_session(session_id="session:stale-turn", goal="Configure inbound")
    repository.update_revision(
        session_id="session:stale-turn",
        expected_revision=1,
        state_update={"status": SessionStatus.PLANNING.value},
        actor="planning",
        reason="advance_revision",
    )

    with pytest.raises(SessionRevisionConflict):
        repository.append_turn(
            session_id="session:stale-turn",
            expected_revision=1,
            role="user",
            message="Stale turn",
        )
    assert repository.list_turns("session:stale-turn") == ()


def test_revision_write_rolls_back_if_session_pointer_update_fails(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    repository = _repository(database)
    repository.create_session(session_id="session:rollback", goal="Configure inbound")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER force_session_update_failure
            BEFORE UPDATE ON sessions
            BEGIN
                SELECT RAISE(ABORT, 'forced update failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced update failure"):
        repository.update_revision(
            session_id="session:rollback",
            expected_revision=1,
            state_update={"status": SessionStatus.PLANNING.value},
            actor="planning",
            reason="trigger_rollback",
        )

    assert repository.get_session("session:rollback").current_revision == 1
    assert [item.revision for item in repository.list_revisions("session:rollback")] == [1]


def test_revision_update_preserves_system_managed_identity_and_creation_time(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "sessions.db")
    original = repository.create_session(
        session_id="session:system-fields", goal="Configure inbound"
    )

    revision = repository.update_revision(
        session_id=original.session_id,
        expected_revision=1,
        state_update={
            "session_id": "session:attempted-overwrite",
            "revision": 99,
            "created_at": "2099-01-01T00:00:00Z",
            "updated_at": "2099-01-01T00:00:00Z",
            "status": SessionStatus.PLANNING.value,
        },
        actor="planning",
        reason="protect_system_fields",
    )

    assert revision.session_id == original.session_id
    assert revision.revision == 2
    assert revision.state["session_id"] == original.session_id
    assert revision.state["revision"] == 2
    assert revision.state["created_at"] == original.created_at
    assert revision.state["updated_at"] != "2099-01-01T00:00:00Z"


def test_restart_recovers_session_revision_and_turns(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    service = SessionService(_repository(database))
    session = service.create_session("Configure inbound", session_id="session:restart")
    service.append_turn(
        session.session_id,
        expected_revision=1,
        role="user",
        message="Use product version 2024.1",
        metadata={"channel": "mcp"},
    )
    service.update_revision(
        session.session_id,
        expected_revision=1,
        state_update={
            "status": SessionStatus.COLLECTING_REQUIREMENTS.value,
            "confirmed_context": {"product_version": "2024.1"},
        },
        actor="requirement",
        reason="version_confirmed",
    )

    restarted = _repository(database)
    recovered_session = restarted.get_session("session:restart")
    recovered_revision = restarted.get_revision("session:restart")
    recovered_turns = restarted.list_turns("session:restart")

    assert recovered_session.current_revision == 2
    assert recovered_revision.state["confirmed_context"] == {"product_version": "2024.1"}
    assert recovered_revision.fingerprint == state_fingerprint(recovered_revision.state)
    assert recovered_turns[0].metadata == {"channel": "mcp"}


def test_cancel_is_a_versioned_terminal_transition(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "sessions.db")
    repository.create_session(session_id="session:cancel", goal="Configure inbound")

    cancelled = repository.cancel_session(
        session_id="session:cancel",
        expected_revision=1,
        actor="user",
        reason="requirements_withdrawn",
    )

    assert cancelled.status is SessionStatus.CANCELLED
    assert cancelled.current_revision == 2
    assert cancelled.cancelled_at is not None
    assert repository.get_revision("session:cancel", 2).status is SessionStatus.CANCELLED
    with pytest.raises(SessionRepositoryError, match="terminal"):
        repository.append_turn(
            session_id="session:cancel",
            expected_revision=2,
            role="user",
            message="This must be rejected",
        )


def test_decision_approval_and_export_records_round_trip(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "sessions.db")
    repository.create_session(session_id="session:records", goal="Configure inbound")
    decision = Decision(
        decision_id="decision:version",
        summary="Use version 2024.1",
        rationale="The target environment is 2024.1",
        source_turn_id="turn:version",
    )
    artifact = ExportArtifact(
        format="json",
        path="data/exports/session-records.json",
        fingerprint="a" * 64,
    )

    repository.record_decision(session_id="session:records", expected_revision=1, decision=decision)
    repository.record_approval(
        session_id="session:records",
        expected_revision=1,
        decision=ReviewDecision.REVISE,
        actor="reviewer",
        comment="Add rollback evidence.",
        approval_id="approval:one",
    )
    repository.record_export(
        session_id="session:records",
        expected_revision=1,
        artifact=artifact,
        export_id="export:one",
    )

    assert repository.list_decisions("session:records")[0].decision == decision
    assert repository.list_approvals("session:records")[0].decision is ReviewDecision.REVISE
    assert repository.list_exports("session:records")[0].artifact == artifact


def test_duplicate_session_creation_is_atomic(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "sessions.db")
    repository.create_session(session_id="session:duplicate", goal="First goal")

    with pytest.raises(SessionAlreadyExistsError):
        repository.create_session(session_id="session:duplicate", goal="Second goal")

    assert repository.get_session("session:duplicate").goal == "First goal"
    assert len(repository.list_revisions("session:duplicate")) == 1


def test_session_service_exposes_get_and_cancel_use_cases(tmp_path: Path) -> None:
    service = SessionService(_repository(tmp_path / "sessions.db"))
    created = service.create_session("Configure inbound", session_id="session:service")

    assert service.get_session(created.session_id) == created
    cancelled = service.cancel_session(
        created.session_id,
        expected_revision=1,
        actor="user",
        reason="cancel_service_test",
    )

    assert cancelled.status is SessionStatus.CANCELLED
    assert service.get_session(created.session_id).current_revision == 2
