"""Transactional SQLite repository for versioned configuration sessions."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.contracts import (
    ConfigurationSessionState,
    Decision,
    ExportArtifact,
    ReviewDecision,
    SessionStatus,
    canonical_json,
    state_fingerprint,
)

_TURN_ROLES = frozenset({"user", "assistant", "system", "tool"})
_TERMINAL_SESSION_STATUSES = frozenset({SessionStatus.CANCELLED})


class SessionRepositoryError(RuntimeError):
    """Base class for durable session repository failures."""


class SessionAlreadyExistsError(SessionRepositoryError):
    """Raised when a session identifier has already been persisted."""


class SessionNotFoundError(SessionRepositoryError):
    """Raised when a requested session does not exist."""


class SessionRevisionConflict(SessionRepositoryError):
    """Raised when optimistic revision protection rejects a stale write."""

    def __init__(self, session_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"Session {session_id!r} revision conflict: expected {expected}, actual {actual}"
        )
        self.session_id = session_id
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    goal: str
    status: SessionStatus
    current_revision: int
    checkpoint_thread_id: str
    created_at: str
    updated_at: str
    cancelled_at: str | None


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    session_id: str
    revision: int
    status: SessionStatus
    state: ConfigurationSessionState
    fingerprint: str
    actor: str
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_id: str
    session_id: str
    revision: int
    sequence: int
    role: str
    message: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class StoredDecisionRecord:
    session_id: str
    revision: int
    decision: Decision
    created_at: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    session_id: str
    revision: int
    decision: ReviewDecision
    actor: str
    comment: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ExportRecord:
    export_id: str
    session_id: str
    revision: int
    artifact: ExportArtifact
    created_at: str


class SessionRepository:
    """Persist versioned business sessions independently from graph checkpoints."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        if not self.database_path.name:
            raise ValueError("database_path must identify a SQLite file")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._initialize_schema()

    def create_session(
        self,
        *,
        session_id: str,
        goal: str,
        initial_state: ConfigurationSessionState | None = None,
        actor: str = "system",
        reason: str = "session_created",
    ) -> SessionRecord:
        """Create a session and its immutable revision-one snapshot atomically."""

        session_id = _required_text(session_id, "session_id")
        goal = _required_text(goal, "goal")
        actor = _required_text(actor, "actor")
        reason = _required_text(reason, "reason")
        timestamp = self._timestamp()
        state = dict(initial_state or {})
        state.update(
            {
                "session_id": session_id,
                "revision": 1,
                "status": SessionStatus.CREATED.value,
                "created_at": timestamp,
                "updated_at": timestamp,
                "user_goal": goal,
            }
        )
        state_json = canonical_json(state)
        fingerprint = state_fingerprint(state)

        try:
            with self._write_transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, goal, status, current_revision,
                        checkpoint_thread_id, created_at, updated_at, cancelled_at
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, NULL)
                    """,
                    (
                        session_id,
                        goal,
                        SessionStatus.CREATED.value,
                        session_id,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO revisions (
                        session_id, revision, status, state_json, state_fingerprint,
                        actor, reason, created_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        SessionStatus.CREATED.value,
                        state_json,
                        fingerprint,
                        actor,
                        reason,
                        timestamp,
                    ),
                )
                row = self._select_session(connection, session_id)
        except sqlite3.IntegrityError as exc:
            raise SessionAlreadyExistsError(f"Session already exists: {session_id}") from exc
        return _session_from_row(row)

    def get_session(self, session_id: str) -> SessionRecord:
        session_id = _required_text(session_id, "session_id")
        with self._read_connection() as connection:
            row = self._select_session(connection, session_id)
        return _session_from_row(row)

    def get_revision(self, session_id: str, revision: int | None = None) -> RevisionRecord:
        session_id = _required_text(session_id, "session_id")
        with self._read_connection() as connection:
            if revision is None:
                session = self._select_session(connection, session_id)
                revision = int(session["current_revision"])
            else:
                _validate_revision(revision)
            row = connection.execute(
                """
                SELECT session_id, revision, status, state_json, state_fingerprint,
                       actor, reason, created_at
                FROM revisions
                WHERE session_id = ? AND revision = ?
                """,
                (session_id, revision),
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"Session revision does not exist: {session_id}@{revision}")
        return _revision_from_row(row)

    def append_turn(
        self,
        *,
        session_id: str,
        expected_revision: int,
        role: str,
        message: str,
        metadata: dict[str, Any] | None = None,
        turn_id: str | None = None,
    ) -> TurnRecord:
        """Append one conversation turn under optimistic revision protection."""

        session_id = _required_text(session_id, "session_id")
        _validate_revision(expected_revision)
        role = _required_text(role, "role").lower()
        if role not in _TURN_ROLES:
            raise ValueError(f"role must be one of: {', '.join(sorted(_TURN_ROLES))}")
        message = _required_text(message, "message")
        metadata = dict(metadata or {})
        metadata_json = canonical_json(metadata)
        turn_id = _required_text(
            turn_id if turn_id is not None else f"turn:{uuid.uuid4().hex}", "turn_id"
        )
        timestamp = self._timestamp()

        with self._write_transaction() as connection:
            session = self._require_expected_revision(
                connection, session_id, expected_revision, allow_terminal=False
            )
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM turns WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO turns (
                    turn_id, session_id, revision, sequence, role,
                    message, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    expected_revision,
                    sequence,
                    role,
                    message,
                    metadata_json,
                    timestamp,
                ),
            )
            self._touch_session(connection, session_id, expected_revision, timestamp)
            row = connection.execute("SELECT * FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()
            if row is None or session is None:  # pragma: no cover - protected by transaction
                raise SessionRepositoryError("Failed to read the inserted turn")
        return _turn_from_row(row)

    def update_revision(
        self,
        *,
        session_id: str,
        expected_revision: int,
        state_update: ConfigurationSessionState,
        actor: str,
        reason: str,
    ) -> RevisionRecord:
        """Merge a state update into a new immutable revision atomically."""

        session_id = _required_text(session_id, "session_id")
        _validate_revision(expected_revision)
        actor = _required_text(actor, "actor")
        reason = _required_text(reason, "reason")
        timestamp = self._timestamp()

        with self._write_transaction() as connection:
            session = self._require_expected_revision(
                connection, session_id, expected_revision, allow_terminal=False
            )
            current = self._select_revision(connection, session_id, expected_revision)
            current_state = _decode_json_object(current["state_json"], "revision.state_json")
            next_revision = expected_revision + 1
            next_state = dict(current_state)
            next_state.update(dict(state_update))
            next_state.update(
                {
                    "session_id": session_id,
                    "revision": next_revision,
                    "created_at": current_state["created_at"],
                    "updated_at": timestamp,
                }
            )
            status = _session_status(next_state.get("status", session["status"]))
            next_state["status"] = status.value
            goal = _required_text(next_state.get("user_goal", session["goal"]), "user_goal")
            state_json = canonical_json(next_state)
            fingerprint = state_fingerprint(next_state)
            connection.execute(
                """
                INSERT INTO revisions (
                    session_id, revision, status, state_json, state_fingerprint,
                    actor, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    next_revision,
                    status.value,
                    state_json,
                    fingerprint,
                    actor,
                    reason,
                    timestamp,
                ),
            )
            cancelled_at = timestamp if status is SessionStatus.CANCELLED else None
            cursor = connection.execute(
                """
                UPDATE sessions
                SET goal = ?, status = ?, current_revision = ?, updated_at = ?,
                    cancelled_at = COALESCE(?, cancelled_at)
                WHERE session_id = ? AND current_revision = ?
                """,
                (
                    goal,
                    status.value,
                    next_revision,
                    timestamp,
                    cancelled_at,
                    session_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                actual = self._select_session(connection, session_id)["current_revision"]
                raise SessionRevisionConflict(session_id, expected_revision, int(actual))
            row = self._select_revision(connection, session_id, next_revision)
        return _revision_from_row(row)

    def cancel_session(
        self,
        *,
        session_id: str,
        expected_revision: int,
        actor: str,
        reason: str,
    ) -> SessionRecord:
        self.update_revision(
            session_id=session_id,
            expected_revision=expected_revision,
            state_update={"status": SessionStatus.CANCELLED.value},
            actor=actor,
            reason=reason,
        )
        return self.get_session(session_id)

    def record_decision(
        self,
        *,
        session_id: str,
        expected_revision: int,
        decision: Decision,
    ) -> StoredDecisionRecord:
        session_id = _required_text(session_id, "session_id")
        _validate_revision(expected_revision)
        if not isinstance(decision, Decision):
            raise ValueError("decision must be a Decision")
        timestamp = self._timestamp()
        with self._write_transaction() as connection:
            self._require_expected_revision(
                connection, session_id, expected_revision, allow_terminal=False
            )
            connection.execute(
                """
                INSERT INTO decisions (
                    decision_id, session_id, revision, decision_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    session_id,
                    expected_revision,
                    decision.to_json(),
                    timestamp,
                ),
            )
            self._touch_session(connection, session_id, expected_revision, timestamp)
        return StoredDecisionRecord(session_id, expected_revision, decision, timestamp)

    def record_approval(
        self,
        *,
        session_id: str,
        expected_revision: int,
        decision: ReviewDecision,
        actor: str,
        comment: str,
        approval_id: str | None = None,
    ) -> ApprovalRecord:
        session_id = _required_text(session_id, "session_id")
        _validate_revision(expected_revision)
        if not isinstance(decision, ReviewDecision):
            raise ValueError("decision must be a ReviewDecision")
        actor = _required_text(actor, "actor")
        comment = _required_text(comment, "comment")
        approval_id = _required_text(approval_id or f"approval:{uuid.uuid4().hex}", "approval_id")
        timestamp = self._timestamp()
        with self._write_transaction() as connection:
            self._require_expected_revision(
                connection, session_id, expected_revision, allow_terminal=False
            )
            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id, session_id, revision, decision, actor, comment, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    session_id,
                    expected_revision,
                    decision.value,
                    actor,
                    comment,
                    timestamp,
                ),
            )
            self._touch_session(connection, session_id, expected_revision, timestamp)
        return ApprovalRecord(
            approval_id,
            session_id,
            expected_revision,
            decision,
            actor,
            comment,
            timestamp,
        )

    def record_export(
        self,
        *,
        session_id: str,
        expected_revision: int,
        artifact: ExportArtifact,
        export_id: str | None = None,
    ) -> ExportRecord:
        session_id = _required_text(session_id, "session_id")
        _validate_revision(expected_revision)
        if not isinstance(artifact, ExportArtifact):
            raise ValueError("artifact must be an ExportArtifact")
        export_id = _required_text(export_id or f"export:{uuid.uuid4().hex}", "export_id")
        timestamp = self._timestamp()
        with self._write_transaction() as connection:
            self._require_expected_revision(
                connection, session_id, expected_revision, allow_terminal=False
            )
            connection.execute(
                """
                INSERT INTO exports (
                    export_id, session_id, revision, artifact_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    export_id,
                    session_id,
                    expected_revision,
                    artifact.to_json(),
                    timestamp,
                ),
            )
            self._touch_session(connection, session_id, expected_revision, timestamp)
        return ExportRecord(export_id, session_id, expected_revision, artifact, timestamp)

    def apply_review_transition(
        self,
        *,
        session_id: str,
        expected_revision: int,
        decision: ReviewDecision,
        state_update: ConfigurationSessionState,
        actor: str,
        comment: str,
        approval_id: str,
    ) -> RevisionRecord:
        """Persist approval and its resulting revision in one transaction."""

        session_id = _required_text(session_id, "session_id")
        _validate_revision(expected_revision)
        actor = _required_text(actor, "actor")
        comment = _required_text(comment, "comment")
        approval_id = _required_text(approval_id, "approval_id")
        if not isinstance(decision, ReviewDecision):
            raise ValueError("decision must be a ReviewDecision")
        timestamp = self._timestamp()
        with self._write_transaction() as connection:
            session = self._require_expected_revision(
                connection, session_id, expected_revision, allow_terminal=False
            )
            current = self._select_revision(connection, session_id, expected_revision)
            current_state = _decode_json_object(current["state_json"], "revision.state_json")
            next_revision = expected_revision + 1
            next_state = {**current_state, **dict(state_update)}
            next_state.update(
                {
                    "session_id": session_id,
                    "revision": next_revision,
                    "created_at": current_state["created_at"],
                    "updated_at": timestamp,
                }
            )
            status = _session_status(next_state.get("status", session["status"]))
            next_state["status"] = status.value
            goal = _required_text(next_state.get("user_goal", session["goal"]), "user_goal")
            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id, session_id, revision, decision, actor, comment, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    session_id,
                    expected_revision,
                    decision.value,
                    actor,
                    comment,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO revisions (
                    session_id, revision, status, state_json, state_fingerprint,
                    actor, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    next_revision,
                    status.value,
                    canonical_json(next_state),
                    state_fingerprint(next_state),
                    actor,
                    f"review:{decision.value}",
                    timestamp,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE sessions
                SET goal = ?, status = ?, current_revision = ?, updated_at = ?
                WHERE session_id = ? AND current_revision = ?
                """,
                (goal, status.value, next_revision, timestamp, session_id, expected_revision),
            )
            if cursor.rowcount != 1:
                actual = self._select_session(connection, session_id)["current_revision"]
                raise SessionRevisionConflict(session_id, expected_revision, int(actual))
            row = self._select_revision(connection, session_id, next_revision)
        return _revision_from_row(row)

    def list_turns(self, session_id: str) -> tuple[TurnRecord, ...]:
        with self._read_connection() as connection:
            self._select_session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
        return tuple(_turn_from_row(row) for row in rows)

    def list_revisions(self, session_id: str) -> tuple[RevisionRecord, ...]:
        with self._read_connection() as connection:
            self._select_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT session_id, revision, status, state_json, state_fingerprint,
                       actor, reason, created_at
                FROM revisions WHERE session_id = ? ORDER BY revision
                """,
                (session_id,),
            ).fetchall()
        return tuple(_revision_from_row(row) for row in rows)

    def list_decisions(self, session_id: str) -> tuple[StoredDecisionRecord, ...]:
        with self._read_connection() as connection:
            self._select_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT session_id, revision, decision_json, created_at
                FROM decisions WHERE session_id = ? ORDER BY created_at, decision_id
                """,
                (session_id,),
            ).fetchall()
        return tuple(_decision_from_row(row) for row in rows)

    def list_approvals(self, session_id: str) -> tuple[ApprovalRecord, ...]:
        with self._read_connection() as connection:
            self._select_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT approval_id, session_id, revision, decision, actor, comment, created_at
                FROM approvals WHERE session_id = ? ORDER BY created_at, approval_id
                """,
                (session_id,),
            ).fetchall()
        return tuple(_approval_from_row(row) for row in rows)

    def list_exports(self, session_id: str) -> tuple[ExportRecord, ...]:
        with self._read_connection() as connection:
            self._select_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT export_id, session_id, revision, artifact_json, created_at
                FROM exports WHERE session_id = ? ORDER BY created_at, export_id
                """,
                (session_id,),
            ).fetchall()
        return tuple(_export_from_row(row) for row in rows)

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        for attempt in range(8):
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                _create_schema(connection)
                return
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.rollback()
                if "locked" not in str(exc).casefold() or attempt == 7:
                    raise
                time.sleep(0.02 * (2**attempt))
            finally:
                connection.close()

    @staticmethod
    def _select_session(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"Session does not exist: {session_id}")
        return row

    @staticmethod
    def _select_revision(
        connection: sqlite3.Connection, session_id: str, revision: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM revisions WHERE session_id = ? AND revision = ?",
            (session_id, revision),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"Session revision does not exist: {session_id}@{revision}")
        return row

    def _require_expected_revision(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        expected_revision: int,
        *,
        allow_terminal: bool,
    ) -> sqlite3.Row:
        session = self._select_session(connection, session_id)
        actual = int(session["current_revision"])
        if actual != expected_revision:
            raise SessionRevisionConflict(session_id, expected_revision, actual)
        status = _session_status(session["status"])
        if not allow_terminal and status in _TERMINAL_SESSION_STATUSES:
            raise SessionRepositoryError(f"Session is terminal: {session_id} ({status.value})")
        return session

    @staticmethod
    def _touch_session(
        connection: sqlite3.Connection,
        session_id: str,
        expected_revision: int,
        timestamp: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE sessions SET updated_at = ?
            WHERE session_id = ? AND current_revision = ?
            """,
            (timestamp, session_id, expected_revision),
        )
        if cursor.rowcount != 1:
            row = SessionRepository._select_session(connection, session_id)
            raise SessionRevisionConflict(
                session_id, expected_revision, int(row["current_revision"])
            )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        BEGIN IMMEDIATE;

        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
            checkpoint_thread_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            cancelled_at TEXT
        );

        CREATE TABLE IF NOT EXISTS revisions (
            session_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            status TEXT NOT NULL,
            state_json TEXT NOT NULL,
            state_fingerprint TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (session_id, revision),
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS turns (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (session_id, sequence),
            FOREIGN KEY (session_id, revision)
                REFERENCES revisions(session_id, revision) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            decision_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id, revision)
                REFERENCES revisions(session_id, revision) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            decision TEXT NOT NULL,
            actor TEXT NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id, revision)
                REFERENCES revisions(session_id, revision) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS exports (
            export_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            artifact_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id, revision)
                REFERENCES revisions(session_id, revision) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session_revision
            ON turns(session_id, revision);
        CREATE INDEX IF NOT EXISTS idx_decisions_session_revision
            ON decisions(session_id, revision);
        CREATE INDEX IF NOT EXISTS idx_approvals_session_revision
            ON approvals(session_id, revision);
        CREATE INDEX IF NOT EXISTS idx_exports_session_revision
            ON exports(session_id, revision);

        PRAGMA user_version=1;

        COMMIT;
        """
    )


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        session_id=str(row["session_id"]),
        goal=str(row["goal"]),
        status=_session_status(row["status"]),
        current_revision=int(row["current_revision"]),
        checkpoint_thread_id=str(row["checkpoint_thread_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        cancelled_at=str(row["cancelled_at"]) if row["cancelled_at"] is not None else None,
    )


def _revision_from_row(row: sqlite3.Row) -> RevisionRecord:
    return RevisionRecord(
        session_id=str(row["session_id"]),
        revision=int(row["revision"]),
        status=_session_status(row["status"]),
        state=_decode_json_object(row["state_json"], "revision.state_json"),
        fingerprint=str(row["state_fingerprint"]),
        actor=str(row["actor"]),
        reason=str(row["reason"]),
        created_at=str(row["created_at"]),
    )


def _turn_from_row(row: sqlite3.Row) -> TurnRecord:
    return TurnRecord(
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        revision=int(row["revision"]),
        sequence=int(row["sequence"]),
        role=str(row["role"]),
        message=str(row["message"]),
        metadata=_decode_json_object(row["metadata_json"], "turn.metadata_json"),
        created_at=str(row["created_at"]),
    )


def _decision_from_row(row: sqlite3.Row) -> StoredDecisionRecord:
    payload = _decode_json_object(row["decision_json"], "decision.decision_json")
    try:
        decision = Decision(**payload)
    except (TypeError, ValueError) as exc:
        raise SessionRepositoryError("Invalid persisted decision contract") from exc
    return StoredDecisionRecord(
        session_id=str(row["session_id"]),
        revision=int(row["revision"]),
        decision=decision,
        created_at=str(row["created_at"]),
    )


def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
    try:
        decision = ReviewDecision(str(row["decision"]))
    except ValueError as exc:
        raise SessionRepositoryError("Invalid persisted approval decision") from exc
    return ApprovalRecord(
        approval_id=str(row["approval_id"]),
        session_id=str(row["session_id"]),
        revision=int(row["revision"]),
        decision=decision,
        actor=str(row["actor"]),
        comment=str(row["comment"]),
        created_at=str(row["created_at"]),
    )


def _export_from_row(row: sqlite3.Row) -> ExportRecord:
    payload = _decode_json_object(row["artifact_json"], "export.artifact_json")
    try:
        artifact = ExportArtifact(**payload)
    except (TypeError, ValueError) as exc:
        raise SessionRepositoryError("Invalid persisted export contract") from exc
    return ExportRecord(
        export_id=str(row["export_id"]),
        session_id=str(row["session_id"]),
        revision=int(row["revision"]),
        artifact=artifact,
        created_at=str(row["created_at"]),
    )


def _decode_json_object(value: Any, field_path: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise SessionRepositoryError(f"Invalid JSON in {field_path}") from exc
    if not isinstance(payload, dict):
        raise SessionRepositoryError(f"{field_path} must contain a JSON object")
    return payload


def _session_status(value: Any) -> SessionStatus:
    try:
        return SessionStatus(str(value))
    except ValueError as exc:
        raise SessionRepositoryError(f"Invalid persisted session status: {value!r}") from exc


def _required_text(value: Any, field_path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_path} must be a non-empty string")
    return value.strip()


def _validate_revision(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_revision must be an integer greater than 0")
