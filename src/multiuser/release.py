"""Offline maintenance/rollback preparation. Never silently re-enable legacy writers."""

import os
import subprocess
import uuid
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from multiuser.migration import file_digest

TRANSITIONS = {
    "active": {"draining"},
    "draining": {"frozen"},
    "frozen": {"rollback_readonly"},
    "rollback_readonly": set(),
}


def inspect_release(cursor):
    state = cursor.execute("SELECT * FROM agent_business.release_state WHERE singleton").fetchone()
    counts = cursor.execute(
        "SELECT (SELECT count(*) FROM agent_business.runs WHERE status IN "
        "('queued','running','cancelling')) AS active_runs,"
        "(SELECT count(*) FROM agent_business.usage_attempts WHERE "
        "status='inflight' OR source='unknown') AS unresolved_attempts"
    ).fetchone()
    return {**state, **counts}


def prepare_phase(connection, target, expected_revision, operator_id, reason, *, apply=False):
    if target not in {"draining", "frozen", "rollback_readonly"} or len(reason.strip()) < 5:
        raise ValueError("Explicit maintenance phase and reason required")
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SET LOCAL ROLE p1_migrator")
        if not cursor.execute(
            "SELECT 1 FROM identity_business.users WHERE user_id=%s AND is_platform_admin",
            (uuid.UUID(operator_id),),
        ).fetchone():
            raise PermissionError("Offline operator required")
        cursor.execute("SELECT * FROM agent_business.release_state WHERE singleton FOR UPDATE")
        state = inspect_release(cursor)
        if state["revision"] != expected_revision:
            raise ValueError("Release state changed; refresh")
        if target not in TRANSITIONS[state["phase"]]:
            raise ValueError("Unsafe release transition")
        if target != "draining" and (state["active_runs"] or state["unresolved_attempts"]):
            raise ValueError("Drain runs and reconcile uncertain calls first")
        if apply:
            cursor.execute(
                "UPDATE agent_business.release_state SET phase=%s,revision=revision+1,"
                "reason=%s,changed_at=clock_timestamp() WHERE singleton",
                (target, reason),
            )
            cursor.execute(
                "INSERT INTO agent_business.management_audit "
                "(audit_id,actor_user_id,target_user_id,action,resource_id,reason) "
                "VALUES(%s,%s,%s,'release_phase',%s,%s)",
                (uuid.uuid4().hex, operator_id, operator_id, target, reason),
            )
        return {"phase": target, "applied": apply, "previous_revision": state["revision"]}


def preserve_post_cutover(dsn, destination, *, workers_stopped=False, runner=subprocess.run):
    """Whole PG database includes new conversation/checkpoint/ledger records; no restore here."""
    if not workers_stopped:
        raise ValueError("Stop all execution/control writers before preserving data")
    path = Path(destination).resolve()
    if path.exists():
        raise ValueError("New private backup destination required")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = conninfo_to_dict(dsn)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    names = {
        "host": "PGHOST",
        "hostaddr": "PGHOSTADDR",
        "port": "PGPORT",
        "dbname": "PGDATABASE",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "passfile": "PGPASSFILE",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
        "connect_timeout": "PGCONNECT_TIMEOUT",
    }
    if set(info) - set(names) or not all(info.get(k) for k in ("host", "dbname", "user")):
        raise ValueError("Explicit supported database/TLS configuration required")
    # Credentials stay in child environment, never argv or output. Operators protect that host.
    for source, target in names.items():
        if source in info:
            env[target] = info[source]
    with path.open("xb") as output:
        result = runner(
            [
                "pg_dump",
                "--format=custom",
                "--no-password",
                "--role=p1_migrator",
                "--enable-row-security",
                "--schema=identity_business",
                "--schema=agent_business",
                "--schema=agent_checkpoints",
            ],
            env=env,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode or path.stat().st_size == 0:
        raise RuntimeError("Database preservation failed; partial file is not a valid backup")
    return {
        "sha256": file_digest(path),
        "bytes": path.stat().st_size,
        "restore_mode": "separate_database_only",
        "legacy_writes_allowed": False,
    }
