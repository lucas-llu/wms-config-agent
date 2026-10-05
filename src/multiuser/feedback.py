"""Feedback keeps the same domain contract through the owner-scoped connection."""

import uuid

from agents.repositories.feedback_repository import (
    FEEDBACK_KINDS,
    REGENERATION_REASONS,
    FeedbackRepository,
)


class PostgresFeedbackRepository(FeedbackRepository):
    def __init__(self, sessions):
        self.sessions = sessions

    def record(self, session_id, revision, kind, reason=""):
        if (
            kind not in FEEDBACK_KINDS
            or (kind == "regeneration" and reason not in REGENERATION_REASONS)
            or (kind != "regeneration" and reason)
        ):
            raise ValueError("Unsupported feedback")
        snapshot = self.sessions.get_revision(session_id, revision)
        with self.sessions._write_transaction() as connection:
            connection.execute(
                "INSERT INTO feedback_signals VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(workspace_id,session_id,revision,kind,reason) DO NOTHING",
                (
                    uuid.uuid4().hex,
                    self.sessions.workspace_id,
                    session_id,
                    revision,
                    None,
                    kind,
                    reason,
                    self.sessions._timestamp(),
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM feedback_signals WHERE session_id=%s "
                    "AND revision=%s AND kind=%s AND reason=%s",
                    (snapshot.session_id, revision, kind, reason),
                ).fetchone()
            )

    def summary(self, session_id, revision):
        self.sessions.get_revision(session_id, revision)
        with self.sessions._read_connection() as connection:
            rows = connection.execute(
                "SELECT kind,reason,COUNT(*) AS count FROM feedback_signals "
                "WHERE session_id=%s AND revision=%s GROUP BY kind,reason",
                (session_id, revision),
            ).fetchall()
            return {"signals": [dict(r) for r in rows], "total": sum(r["count"] for r in rows)}
