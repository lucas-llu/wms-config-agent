"""Private offline SQLite snapshot and explicit, read-only-history PG import.

No checkpoint deserialization, default owner, live source mutation or reverse overwrite.
"""

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from psycopg import sql
from psycopg.rows import dict_row

from agents.contracts import ExportArtifact, SessionStatus, canonical_json, state_fingerprint
from agents.workspace import Workspace
from multiuser.checkpoints import checkpoint_thread

TABLES = (
    "sessions",
    "revisions",
    "turns",
    "decisions",
    "approvals",
    "exports",
    "deleted_sessions",
    "conversation_titles",
    "feedback_signals",
)
OPTIONAL = {"deleted_sessions", "conversation_titles", "feedback_signals"}


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_database(path):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Source integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Source referential integrity check failed")
        if connection.execute("PRAGMA user_version").fetchone()[0] not in {1, 2}:
            raise ValueError("Unsupported source schema")
        names = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        result = {}
        for table in TABLES:
            if table not in names:
                if table in OPTIONAL:
                    result[table] = []
                    continue
                raise ValueError("Incomplete source schema")
            rows = [dict(r) for r in connection.execute(f'SELECT * FROM "{table}"')]
            result[table] = sorted(rows, key=canonical_json)
        return result


def snapshot(source, destination, *, writers_stopped=False, checkpoints=None):
    """Caller must stop ALL source writers; backup preserves WAL content consistently."""
    if not writers_stopped:
        raise ValueError("Confirm source writers stopped before snapshot")
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination == source or destination.exists():
        raise ValueError("New private destination required; never overwrite")
    destination.mkdir(parents=True, mode=0o700)
    for name, path in (("business.sqlite", source), ("checkpoints.sqlite", checkpoints)):
        if path is None:
            continue
        path = Path(path).resolve(strict=True)
        with (
            closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(destination / name)) as copy,
        ):
            original.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
    records = read_database(destination / "business.sqlite")
    manifest = {
        "version": 1,
        "business_hash": digest(records),
        "files": {
            name: file_digest(destination / name)
            for name in ("business.sqlite", "checkpoints.sqlite")
            if (destination / name).is_file()
        },
        "counts": {t: len(rows) for t, rows in records.items()},
        "checkpoint_mode": "archive_only",
        "source_writers_stopped": True,
    }
    (destination / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    return manifest


def load_snapshot(directory):
    directory = Path(directory).resolve(strict=True)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or not manifest.get("source_writers_stopped"):
        raise ValueError("Invalid snapshot manifest")
    for name, expected in manifest["files"].items():
        if name not in {"business.sqlite", "checkpoints.sqlite"}:
            raise ValueError("Unexpected snapshot file")
        path = directory / name
        if path.is_symlink() or file_digest(path) != expected:
            raise ValueError("Snapshot checksum mismatch")
    records = read_database(directory / "business.sqlite")
    if digest(records) != manifest["business_hash"]:
        raise ValueError("Snapshot content mismatch")
    return manifest, records


def project(records, assignments, policies, identities):
    """No inferred provenance: incomplete/out-of-scope evidence stays quarantined."""
    if not isinstance(assignments, dict) or set(assignments) - {
        s["session_id"] for s in records["sessions"]
    }:
        raise ValueError("Ownership plan references unknown sessions")
    projected = {t: [] for t in TABLES}
    receipts, quarantine = [], []
    for session in records["sessions"]:
        sid = session["session_id"]
        assignment = assignments.get(sid)
        if assignment is None:
            quarantine.append({"session_id": sid, "reason": "owner_unassigned"})
            continue
        if (
            set(assignment) != {"user_id", "workspace_id", "reason"}
            or len(assignment["reason"].strip()) < 5
        ):
            raise ValueError("Explicit audited ownership required")
        owner = str(uuid.UUID(assignment["user_id"]))
        workspace = policies.get(assignment["workspace_id"])
        identity = identities.get((owner, assignment["workspace_id"]))
        if workspace is None or workspace.workspace_id == "workspace:legacy" or identity is None:
            quarantine.append({"session_id": sid, "reason": "target_not_authorized"})
            continue
        subset = {
            t: [r.copy() for r in rows if r["session_id"] == sid] for t, rows in records.items()
        }
        original_hash = digest(subset)
        try:
            SessionStatus(session["status"])
            for row in subset["revisions"]:
                state = json.loads(row["state_json"])
                if state.get("session_id") != sid or state.get("revision") != row["revision"]:
                    raise ValueError("Source state binding mismatch")
                if state_fingerprint(state) != row["state_fingerprint"]:
                    raise ValueError("Invalid source fingerprint")
                state["workspace_id"] = workspace.workspace_id
                workspace.validate_state(state)
                row["state_json"] = canonical_json(state)
                row["state_fingerprint"] = state_fingerprint(state)
            for row in subset["turns"]:
                citations = json.loads(row["metadata_json"]).get("citations", [])
                if row["role"] == "assistant" and not citations:
                    raise ValueError("Unstructured historical assistant provenance")
                if any(not workspace.permits_metadata(c) for c in citations):
                    raise ValueError("Unknown citation scope")
            for row in subset["exports"]:
                ExportArtifact(**json.loads(row["artifact_json"]))
            revisions = {r["revision"] for r in subset["revisions"]}
            if revisions != set(range(1, session["current_revision"] + 1)):
                raise ValueError("Missing source revisions")
        except (ValueError, KeyError, TypeError):
            quarantine.append(
                {"session_id": sid, "reason": "history_scope_or_integrity_unverified"}
            )
            continue
        for row in subset["sessions"]:
            row["owner_user_id"] = owner
            row["workspace_id"] = workspace.workspace_id
            row["checkpoint_thread_id"] = checkpoint_thread(identity, sid)
            row["legacy_readonly"] = True
        for row in subset["feedback_signals"]:
            row["workspace_id"] = workspace.workspace_id
        # Never expose old host paths or copy approvals into fresh executable workflows.
        for row in subset["exports"]:
            artifact = json.loads(row["artifact_json"])
            artifact["path"] = "legacy-archive-unavailable/" + digest(row["export_id"])
            row["artifact_json"] = canonical_json(artifact)
        receipts.append(
            {"session_id": sid, "source_hash": original_hash, "projection_hash": digest(subset)}
        )
        for table in TABLES:
            projected[table].extend(subset[table])
    return {
        "records": projected,
        "receipts": receipts,
        "quarantine": quarantine,
        "projection_hash": digest(projected),
    }


def target_catalog(connection, assignments):
    policies, identities = {}, {}
    for assignment in assignments.values():
        user_id = str(uuid.UUID(assignment["user_id"]))
        row = connection.execute(
            "SELECT u.identity_issuer,u.identity_subject,w.policy_json FROM "
            "identity_business.users u JOIN identity_business.memberships m USING(user_id) "
            "JOIN identity_business.workspaces w USING(workspace_id) "
            "WHERE u.user_id=%s AND w.workspace_id=%s AND m.active",
            (user_id, assignment["workspace_id"]),
        ).fetchone()
        if row:
            values = json.loads(row["policy_json"])
            for field in ("collections", "modules", "sites", "environments"):
                values[field] = tuple(values[field])
            policies[values["workspace_id"]] = Workspace(**values)
            identities[(user_id, values["workspace_id"])] = hashlib.sha256(
                f"{row['identity_issuer']}\0{row['identity_subject']}".encode()
            ).hexdigest()
    return policies, identities


def import_snapshot(connection, directory, assignments, operator_id, *, apply=False):
    """Dedicated offline owner role, whole-batch transaction, collision fails closed."""
    manifest, records = load_snapshot(directory)
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SET LOCAL ROLE p1_migrator")
        if not cursor.execute(
            "SELECT 1 FROM identity_business.users WHERE user_id=%s AND is_platform_admin",
            (uuid.UUID(operator_id),),
        ).fetchone():
            raise PermissionError("Existing platform operator required")
        gate = cursor.execute(
            "SELECT phase FROM agent_business.release_state WHERE singleton FOR UPDATE"
        ).fetchone()
        if gate["phase"] != "frozen":
            raise PermissionError("Target must be frozen")
        policies, identities = target_catalog(cursor, assignments)
        projection = project(records, assignments, policies, identities)
        if not apply:
            return {k: v for k, v in projection.items() if k != "records"}
        plan_hash = digest(assignments)
        batch_hash = digest([manifest["business_hash"], plan_hash])
        existing = cursor.execute(
            "SELECT * FROM agent_business.migration_receipts WHERE bundle_hash=%s",
            (batch_hash,),
        ).fetchone()
        if existing:
            if (
                existing["plan_hash"] != plan_hash
                or existing["projection_hash"] != projection["projection_hash"]
            ):
                raise ValueError("Prior import differs; cannot reassign or overwrite")
            return {"status": "already_imported", "imported": existing["imported_count"]}
        previously_imported = set()
        for receipt in projection["receipts"]:
            prior = cursor.execute(
                "SELECT source_hash,projection_hash FROM agent_business.migration_sessions "
                "WHERE session_id=%s",
                (receipt["session_id"],),
            ).fetchone()
            if prior:
                if (
                    prior["source_hash"] != receipt["source_hash"]
                    or prior["projection_hash"] != receipt["projection_hash"]
                ):
                    raise ValueError("Immutable imported ownership/content differs")
                previously_imported.add(receipt["session_id"])
        imported_count = len(projection["receipts"]) - len(previously_imported)
        cursor.execute(
            "INSERT INTO agent_business.migration_receipts "
            "(bundle_hash,plan_hash,operator_id,imported_count,projection_hash) "
            "VALUES(%s,%s,%s,%s,%s)",
            (
                batch_hash,
                plan_hash,
                operator_id,
                imported_count,
                projection["projection_hash"],
            ),
        )
        for table, rows in projection["records"].items():
            columns = {
                r["column_name"]
                for r in cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='agent_business' AND table_name=%s",
                    (table,),
                ).fetchall()
            }
            for row in rows:
                if set(row) - columns:
                    raise ValueError("Unrecognized source columns; require explicit adapter")
                statement = sql.SQL("INSERT INTO agent_business.{} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, row)),
                    sql.SQL(",").join(sql.Placeholder() for _ in row),
                )
                if row["session_id"] not in previously_imported:
                    cursor.execute(statement, tuple(row.values()))
                stored = cursor.execute(
                    sql.SQL("SELECT {} FROM agent_business.{} WHERE {}").format(
                        sql.SQL(",").join(map(sql.Identifier, row)),
                        sql.Identifier(table),
                        sql.SQL(" AND ").join(
                            sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(k))
                            for k in row
                        ),
                    ),
                    tuple(row.values()),
                ).fetchone()
                if stored is None:
                    raise ValueError("Target content verification failed")
        for receipt in projection["receipts"]:
            if receipt["session_id"] in previously_imported:
                continue
            cursor.execute(
                "INSERT INTO agent_business.migration_sessions VALUES(%s,%s,%s,%s)",
                (
                    receipt["session_id"],
                    batch_hash,
                    receipt["source_hash"],
                    receipt["projection_hash"],
                ),
            )
        return {
            "status": "imported",
            "imported": imported_count,
            "quarantined": len(projection["quarantine"]),
            "projection_hash": projection["projection_hash"],
        }
