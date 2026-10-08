"""Audited, transactional installation of the multi-user PostgreSQL schema.

Unlike disposable CI fixture setup, this never activates a release or resets
an existing role password. Existing unversioned or partially installed targets
require an operator audit; they are not silently adopted.
"""

import hashlib
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.rows import dict_row

MIGRATIONS = (
    "001_user_isolation.sql",
    "002_durable_runs.sql",
    "003_run_execution.sql",
    "004_review_dispatch.sql",
    "005_usage_quota.sql",
    "006_authorization_management.sql",
    "007_migration_release.sql",
    "008_release_safety.sql",
)
SCHEMAS = ("agent_business", "agent_checkpoints", "identity_business")
RUNTIME_ROLES = ("p1_runtime", "p3_control")
LOCK_ID = 91845076
CHECKPOINT_MIGRATIONS_SHA256 = "98d38ed91d4a57a2fb066323f26f2902efcb01f483e09f3ff2be31304a799d35"
CHECKPOINT_CONCURRENT_INDEXES = {
    6: (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "checkpoints_thread_id_idx ON checkpoints(thread_id);"
    ),
    7: (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id);"
    ),
    8: (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id);"
    ),
}


def migration_sources(directory):
    root = Path(directory).resolve(strict=True)
    sources = []
    for version, name in enumerate(MIGRATIONS, start=1):
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Migration source escaped the selected directory")
        source = path.read_text(encoding="utf-8")
        sources.append((version, source, hashlib.sha256(source.encode()).hexdigest()))
    return sources


def _role_catalog(connection):
    with connection.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            "SELECT rolname,rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,"
            "rolcreaterole,rolreplication FROM pg_roles WHERE rolname IN "
            "('p1_migrator','p1_runtime','p3_control')"
        ).fetchall()
        roles = {row["rolname"]: row for row in rows}
        for name, row in roles.items():
            if row["rolcanlogin"] != (name != "p1_migrator") or any(
                row[field]
                for field in (
                    "rolsuper",
                    "rolbypassrls",
                    "rolcreatedb",
                    "rolcreaterole",
                    "rolreplication",
                )
            ):
                raise PermissionError("Unexpected migration/runtime role privileges")
        for name in RUNTIME_ROLES:
            if (
                name in roles
                and "p1_migrator" in roles
                and cursor.execute(
                    "SELECT pg_has_role(%s,%s,'MEMBER') AS member", (name, "p1_migrator")
                ).fetchone()["member"]
            ):
                raise PermissionError("Runtime role inherits migration ownership")
        return roles


def _target(connection, expected_database):
    if not expected_database or connection.info.dbname != expected_database:
        raise ValueError("Explicit target database did not match")
    if connection.info.user in {"p1_migrator", *RUNTIME_ROLES}:
        raise PermissionError("A separate schema administrator is required")


def inspect_schema(connection, migration_dir, expected_database):
    """Read-only preflight and post-install audit; does not unlock a frozen release."""
    _target(connection, expected_database)
    sources = migration_sources(migration_dir)
    roles = _role_catalog(connection)
    with connection.cursor(row_factory=dict_row) as cursor:
        schemas = {
            row["nspname"]: row["owner"]
            for row in cursor.execute(
                "SELECT nspname,pg_get_userbyid(nspowner) AS owner FROM pg_namespace "
                "WHERE nspname IN ('agent_business','agent_checkpoints','identity_business')"
            ).fetchall()
        }
        ledger = cursor.execute(
            "SELECT to_regclass('agent_business.schema_migrations') AS present"
        ).fetchone()["present"]
        if not schemas and ledger is None:
            return {"state": "fresh", "pending_versions": [version for version, _, _ in sources]}
        if set(schemas) != set(SCHEMAS) or not all(
            owner == "p1_migrator" for owner in schemas.values()
        ):
            raise ValueError("Unversioned or mismatched business schemas require manual audit")
        if ledger is None or set(roles) != {"p1_migrator", *RUNTIME_ROLES}:
            raise ValueError("Incomplete installation requires manual audit")
        actual = {
            row["version"]: row["checksum"]
            for row in cursor.execute(
                "SELECT version,checksum FROM agent_business.schema_migrations"
            ).fetchall()
        }
        expected = {version: digest for version, _, digest in sources}
        if actual != expected:
            raise ValueError("Migration checksum/version mismatch")
        wrong_owners = cursor.execute(
            "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname IN ('agent_business','agent_checkpoints','identity_business') "
            "AND c.relkind IN ('r','p','S') AND pg_get_userbyid(c.relowner)!='p1_migrator'"
        ).fetchone()["n"]
        if wrong_owners:
            raise PermissionError("Business relation ownership changed")
        for name in (
            "identity_business.users",
            "agent_business.sessions",
            "agent_business.runs",
            "agent_checkpoints.checkpoints",
            "agent_checkpoints.checkpoint_blobs",
            "agent_checkpoints.checkpoint_writes",
        ):
            if not cursor.execute(
                "SELECT c.relrowsecurity AND c.relforcerowsecurity AS protected "
                "FROM pg_class c WHERE c.oid=to_regclass(%s)",
                (name,),
            ).fetchone()["protected"]:
                raise PermissionError("Required row security is missing")
        state = cursor.execute(
            "SELECT phase,revision FROM agent_business.release_state WHERE singleton"
        ).fetchone()
        if state is None:
            raise ValueError("Release state is missing")
        return {
            "state": "installed",
            "versions": sorted(actual),
            "release_phase": state["phase"],
            "release_revision": state["revision"],
            "runtime_roles_non_owner": True,
        }


def _create_roles(connection, existing, runtime_password, control_password):
    if (
        not runtime_password
        or not control_password
        or len(runtime_password) < 24
        or len(control_password) < 24
    ):
        raise ValueError("Strong, separately supplied runtime credentials required")
    if runtime_password == control_password:
        raise ValueError("Runtime and control roles need distinct credentials")
    if "p1_migrator" not in existing:
        connection.execute(
            "CREATE ROLE p1_migrator NOLOGIN NOSUPERUSER NOBYPASSRLS "
            "NOCREATEDB NOCREATEROLE NOREPLICATION"
        )
    for name, password in (("p1_runtime", runtime_password), ("p3_control", control_password)):
        if name not in existing:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION PASSWORD {}"
                ).format(sql.Identifier(name), sql.Literal(password))
            )


def _verify_existing_logins(connection, roles, runtime_password, control_password):
    for name, password in (("p1_runtime", runtime_password), ("p3_control", control_password)):
        if name not in roles:
            continue
        with psycopg.connect(
            connection.info.dsn, user=name, password=password, connect_timeout=5
        ) as probe:
            if probe.execute("SELECT current_user").fetchone()[0] != name:
                raise PermissionError("Runtime database identity mismatch")


def _checkpoint_tables(connection):
    migrations = PostgresSaver.MIGRATIONS
    source_hash = hashlib.sha256("\0".join(migrations).encode()).hexdigest()
    if source_hash != CHECKPOINT_MIGRATIONS_SHA256 or len(migrations) != 10:
        raise RuntimeError("Checkpoint migration source changed; review before installation")
    if any(
        migrations[version].strip() != statement
        for version, statement in CHECKPOINT_CONCURRENT_INDEXES.items()
    ):
        raise RuntimeError("Checkpoint index migration changed")
    connection.execute("SET LOCAL search_path TO agent_checkpoints")
    # These tables are new and invisible until commit. Building their indexes
    # without CONCURRENTLY is equivalent here and keeps the whole install atomic.
    connection.execute(migrations[0])
    for version, statement in enumerate(migrations):
        if version in CHECKPOINT_CONCURRENT_INDEXES:
            statement = statement.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX", 1)
        connection.execute(statement)
        connection.execute("INSERT INTO checkpoint_migrations(v) VALUES(%s)", (version,))
    for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        table = sql.Identifier(name)
        connection.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table))
        connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(table))
        connection.execute(
            sql.SQL(
                "CREATE POLICY private_checkpoints ON {table} USING(EXISTS("
                "SELECT 1 FROM agent_business.checkpoint_threads t "
                "WHERE t.thread_id={table}.thread_id AND ("
                "current_setting('app.purge',true)='1' OR NOT EXISTS("
                "SELECT 1 FROM agent_business.deleted_sessions d "
                "WHERE d.session_id=t.session_id)))) "
                "WITH CHECK(EXISTS(SELECT 1 FROM agent_business.checkpoint_threads t "
                "WHERE t.thread_id={table}.thread_id AND NOT EXISTS("
                "SELECT 1 FROM agent_business.deleted_sessions d "
                "WHERE d.session_id=t.session_id)))"
            ).format(table=table)
        )
    connection.execute(
        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA agent_checkpoints TO p1_runtime"
    )
    connection.execute("SET LOCAL search_path TO pg_catalog,public")


def install_schema(
    connection, migration_dir, expected_database, runtime_password, control_password
):
    """Install all eight migrations atomically; retain the initial frozen state."""
    if (
        not runtime_password
        or not control_password
        or len(runtime_password) < 24
        or len(control_password) < 24
    ):
        raise ValueError("Strong, separately supplied runtime credentials required")
    if runtime_password == control_password:
        raise ValueError("Runtime and control roles need distinct credentials")
    before = inspect_schema(connection, migration_dir, expected_database)
    _verify_existing_logins(
        connection, _role_catalog(connection), runtime_password, control_password
    )
    if before["state"] == "installed":
        return {**before, "applied": False}
    sources = migration_sources(migration_dir)
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        again = inspect_schema(connection, migration_dir, expected_database)
        if again["state"] == "installed":
            return {**again, "applied": False}
        existing = _role_catalog(connection)
        _verify_existing_logins(connection, existing, runtime_password, control_password)
        _create_roles(connection, existing, runtime_password, control_password)
        _role_catalog(connection)
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO p1_runtime,p3_control").format(
                sql.Identifier(expected_database)
            )
        )
        for schema in SCHEMAS:
            connection.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION p1_migrator").format(sql.Identifier(schema))
            )
        connection.execute("SET LOCAL ROLE p1_migrator")
        connection.execute(
            "CREATE TABLE agent_business.schema_migrations "
            "(version integer PRIMARY KEY,checksum text NOT NULL)"
        )
        # 001 creates the three schemas before its own SET LOCAL ROLE. Even
        # IF NOT EXISTS checks database CREATE privilege, so those statements
        # must run under the installer identity, as in the original P1 setup.
        connection.execute("RESET ROLE")
        for version, source, digest in sources:
            if version == 2:
                _checkpoint_tables(connection)
            connection.execute(source, prepare=False)
            connection.execute(
                "INSERT INTO agent_business.schema_migrations(version,checksum) VALUES(%s,%s)",
                (version, digest),
            )
    after = inspect_schema(connection, migration_dir, expected_database)
    if after["state"] != "installed" or after["release_phase"] != "frozen":
        raise RuntimeError("Initial release was not frozen")
    _verify_existing_logins(
        connection, _role_catalog(connection), runtime_password, control_password
    )
    return {**after, "applied": True}
