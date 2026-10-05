"""Private-content-free, revision-bound feedback signals in the session database."""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from agents.repositories.session_repository import SessionRepository

FEEDBACK_KINDS = ("thumbs_up", "thumbs_down", "citation_error", "incomplete_answer", "regeneration")
REGENERATION_REASONS = (
    "citation_error",
    "incomplete_answer",
    "unclear_answer",
    "changed_requirement",
)


class FeedbackRepository:
    def __init__(self, sessions: SessionRepository) -> None:
        self.sessions = sessions
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS feedback_signals (
                    feedback_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    trace_id TEXT,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id, revision) REFERENCES revisions(session_id, revision),
                    UNIQUE(workspace_id, session_id, revision, kind, reason)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sessions.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def record(self, session_id: str, revision: int, kind: str, reason: str = "") -> dict[str, Any]:
        if kind not in FEEDBACK_KINDS:
            raise ValueError("Unsupported feedback kind")
        if (kind == "regeneration" and reason not in REGENERATION_REASONS) or (
            kind != "regeneration" and reason != ""
        ):
            raise ValueError("Reason must be an allowed regeneration reason, otherwise empty")
        snapshot = self.sessions.get_revision(session_id, revision)
        trace = snapshot.state.get("trace_id")
        # Only system-generated trace IDs; never copy arbitrary state text into feedback.
        trace_id = (
            trace if isinstance(trace, str) and re.fullmatch(r"[0-9a-f]{32}", trace) else None
        )
        scope = (self.sessions.workspace_id, snapshot.session_id, snapshot.revision)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO feedback_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, session_id, revision, kind, reason) DO NOTHING""",
                (uuid.uuid4().hex, *scope, trace_id, kind, reason, datetime.now(UTC).isoformat()),
            )
            row = connection.execute(
                """SELECT * FROM feedback_signals WHERE workspace_id = ? AND session_id = ?
                AND revision = ? AND kind = ? AND reason = ?""",
                (*scope, kind, reason),
            ).fetchone()
        return dict(row)

    def summary(self, session_id: str, revision: int) -> dict[str, Any]:
        snapshot = self.sessions.get_revision(session_id, revision)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT kind, reason, COUNT(*) AS count FROM feedback_signals
                WHERE workspace_id = ? AND session_id = ? AND revision = ?
                GROUP BY kind, reason ORDER BY kind, reason""",
                (self.sessions.workspace_id, snapshot.session_id, snapshot.revision),
            ).fetchall()
        return {
            "workspace_id": self.sessions.workspace_id,
            "session_id": snapshot.session_id,
            "revision": snapshot.revision,
            "signals": [dict(row) for row in rows],
            "total": sum(row["count"] for row in rows),
        }
