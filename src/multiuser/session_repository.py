"""User-scoped PostgreSQL storage preserving the existing session contracts.

The SQL bridge is internal only: SQL comes from the existing fixed repository
statements, never an HTTP request. Database RLS independently guards every row.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime

import psycopg

from agents.repositories.session_repository import SessionNotFoundError, SessionRepository
from agents.workspace import Workspace
from multiuser.access import AccessDenied, AccessStore, UserContext


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self.cursor.fetchall()]


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=()):
        query = query.replace("?", "%s")
        if "LIMIT %s" in query and params and params[-1] == -1:
            params = (*params[:-1], None)
        if "INSERT OR IGNORE INTO" in query:
            query = (
                query.replace("INSERT OR IGNORE INTO", "INSERT INTO") + " ON CONFLICT DO NOTHING"
            )
        try:
            cursor = self.connection.execute(query, params)
            if query.lstrip().startswith("INSERT INTO sessions"):
                self.connection.execute(
                    "INSERT INTO checkpoint_threads(thread_id,session_id) VALUES(%s,%s)",
                    (params[0], params[0]),
                )
            return Cursor(cursor)
        except psycopg.errors.InsufficientPrivilege as exc:
            raise AccessDenied("Operation not permitted") from exc
        except psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError("Invalid or duplicate record") from exc


class PostgresSessionRepository(SessionRepository):
    def __init__(self, store: AccessStore, context: UserContext, workspace_id: str):
        if workspace_id == "workspace:legacy":
            raise AccessDenied("Legacy unrestricted scope is not a public user workspace")
        self.store, self.context, self.workspace_id = store, context, workspace_id
        self._clock = lambda: datetime.now(UTC)
        with store.transaction(context) as connection:
            row = connection.execute(
                "SELECT policy_json FROM identity_business.workspaces WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()
            if row is None:
                raise AccessDenied("Workspace membership required")
            values = json.loads(row["policy_json"])
            for field in ("collections", "modules", "sites", "environments"):
                values[field] = tuple(values[field])
            self.workspace = Workspace(**values)

    @contextmanager
    def _read_connection(self):
        with self.store.transaction(self.context) as connection:
            self._check_policy(connection)
            yield Connection(connection)

    @contextmanager
    def _write_transaction(self):
        with self.store.transaction(self.context) as connection:
            self._check_policy(connection)
            yield Connection(connection)

    def _check_policy(self, connection):
        row = connection.execute(
            "SELECT policy_json FROM identity_business.workspaces WHERE workspace_id=%s",
            (self.workspace_id,),
        ).fetchone()
        expected = json.loads(json.dumps(asdict(self.workspace)))
        if row is None or json.loads(row["policy_json"]) != expected:
            raise AccessDenied("Workspace permission changed; restart the request")

    def _select_session(self, connection, session_id, *, include_deleted=False):
        suffix = (
            ""
            if include_deleted
            else " AND NOT EXISTS(SELECT 1 FROM deleted_sessions d WHERE d.session_id=s.session_id)"
        )
        row = connection.execute(
            "SELECT s.*,t.display_title FROM sessions s "
            "LEFT JOIN conversation_titles t ON t.session_id=s.session_id "
            "WHERE s.session_id=%s AND s.workspace_id=%s" + suffix + " FOR UPDATE OF s",
            (session_id, self.workspace_id),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError("Conversation not found")
        # P4 policies are editable: check historical evidence at the common parent gate.
        for saved in connection.execute(
            "SELECT state_json FROM revisions WHERE session_id=%s", (session_id,)
        ).fetchall():
            try:
                self.workspace.validate_state(json.loads(saved["state_json"]))
            except ValueError as exc:
                raise AccessDenied("Conversation evidence is outside current scope") from exc
        for saved in connection.execute(
            "SELECT metadata_json FROM turns WHERE session_id=%s AND role='assistant'",
            (session_id,),
        ).fetchall():
            if any(
                not self.workspace.permits_metadata(citation)
                for citation in json.loads(saved["metadata_json"]).get("citations", [])
            ):
                raise AccessDenied("Conversation evidence is outside current scope")
        return row

    def list_sessions(self, *, limit=100):
        from agents.repositories.session_repository import _session_from_row

        if not 1 <= limit <= 100:
            raise ValueError("Bounded pagination required")
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT s.*,t.display_title FROM sessions s "
                "LEFT JOIN conversation_titles t ON t.session_id=s.session_id "
                "WHERE s.workspace_id=%s AND NOT archived AND NOT EXISTS("
                "SELECT 1 FROM deleted_sessions d WHERE d.session_id=s.session_id) "
                "ORDER BY s.updated_at DESC,s.session_id LIMIT %s",
                (self.workspace_id, limit),
            ).fetchall()
        return tuple(_session_from_row(row) for row in rows)

    def archive(self, session_id, value=True):
        with self._write_transaction() as connection:
            self._select_session(connection, session_id)
            connection.execute(
                "UPDATE sessions SET archived=%s WHERE session_id=%s", (bool(value), session_id)
            )

    def purge_deleted_sessions(self, expected_revisions, *, checkpoint_path=None):
        from agents.repositories import SessionRevisionConflict

        if checkpoint_path is not None:
            raise ValueError("PostgreSQL checkpoint cleanup does not accept a filesystem path")
        if not expected_revisions:
            raise ValueError("Select deleted conversations")
        with self._write_transaction() as connection:
            for session_id, revision in expected_revisions.items():
                row = self._select_session(connection, session_id, include_deleted=True)
                if row["current_revision"] != revision:
                    raise SessionRevisionConflict(session_id, revision, row["current_revision"])
                if not connection.execute(
                    "SELECT 1 FROM deleted_sessions WHERE session_id=%s", (session_id,)
                ).fetchone():
                    raise ValueError("Conversation must be in recycle bin")
            for session_id in expected_revisions:
                connection.execute("SELECT set_config('app.purge','1',true)")
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    connection.execute(
                        f"DELETE FROM agent_checkpoints.{table} WHERE thread_id IN "
                        "(SELECT thread_id FROM checkpoint_threads WHERE session_id=%s)",
                        (session_id,),
                    )
                connection.execute("DELETE FROM sessions WHERE session_id=%s", (session_id,))
        return len(expected_revisions)

    def search_sessions(self, query="", *, archived=False, limit=100):
        from agents.repositories.session_repository import _session_from_row

        if not isinstance(query, str) or len(query) > 200 or not 1 <= limit <= 100:
            raise ValueError("Invalid search")
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT s.*,t.display_title FROM sessions s "
                "LEFT JOIN conversation_titles t ON t.session_id=s.session_id "
                "WHERE s.workspace_id=%s AND archived=%s "
                "AND NOT EXISTS(SELECT 1 FROM deleted_sessions d WHERE d.session_id=s.session_id) "
                "AND strpos(lower(coalesce(t.display_title,s.goal)),lower(%s))>0 "
                "ORDER BY s.updated_at DESC,s.session_id LIMIT %s",
                (self.workspace_id, archived, query, limit),
            ).fetchall()
            return tuple(_session_from_row(row) for row in rows)

    def put_resource(self, session_id, resource_id, kind, payload, storage_key=None):
        from agents.contracts import canonical_json

        with self._write_transaction() as connection:
            self._select_session(connection, session_id)
            connection.execute(
                "INSERT INTO resources(resource_id,session_id,kind,payload_json,storage_key) "
                "VALUES(%s,%s,%s,%s,%s)",
                (resource_id, session_id, kind, canonical_json(payload), storage_key),
            )

    def trash_snapshot(self):
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT s.session_id,s.current_revision FROM sessions s "
                "JOIN deleted_sessions d ON s.session_id=d.session_id "
                "WHERE s.workspace_id=%s ORDER BY s.session_id",
                (self.workspace_id,),
            ).fetchall()
        revisions = {row["session_id"]: row["current_revision"] for row in rows}
        digest = hashlib.sha256(json.dumps(revisions, sort_keys=True).encode()).hexdigest()
        return {"count": len(rows), "fingerprint": digest}, revisions

    def empty_trash(self, fingerprint):
        snapshot, revisions = self.trash_snapshot()
        if snapshot["fingerprint"] != fingerprint:
            from agents.repositories import SessionRevisionConflict

            raise SessionRevisionConflict("trash", 0, snapshot["count"])
        # The existing atomic purge rechecks every owned revision/tombstone.
        # Newly deleted conversations after this snapshot are never included.
        return self.purge_deleted_sessions(revisions) if revisions else 0

    def get_resource(self, session_id, resource_id, kind):
        with self._read_connection() as connection:
            self._select_session(connection, session_id)
            row = connection.execute(
                "SELECT * FROM resources WHERE session_id=%s AND resource_id=%s AND kind=%s",
                (session_id, resource_id, kind),
            ).fetchone()
            if row is None:
                raise SessionNotFoundError("Resource not found")
            return row

    def list_resources(self, session_id, kind, *, limit=100):
        with self._read_connection() as connection:
            self._select_session(connection, session_id)
            return connection.execute(
                "SELECT * FROM resources WHERE session_id=%s AND kind=%s "
                "ORDER BY resource_id LIMIT %s",
                (session_id, kind, limit),
            ).fetchall()
