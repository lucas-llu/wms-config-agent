"""Versioned P3 migration for the disposable CI fixture (not public startup DDL)."""

import hashlib
import os
from pathlib import Path

import psycopg
from psycopg import sql


def prepare():
    if os.getenv("WMS_P3_LIVE") != "1":
        raise RuntimeError("P3 fixture migration requires explicit isolated-test mode")
    with psycopg.connect(os.environ["P0_POSTGRES_DSN"], autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname='p3_control'").fetchone():
            admin.execute(
                "CREATE ROLE p3_control LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
            )
        admin.execute(
            sql.SQL("ALTER ROLE p3_control PASSWORD {}").format(
                sql.Literal(os.environ["P3_DB_PASSWORD"])
            )
        )
    with psycopg.connect(os.environ["P0_POSTGRES_DSN"]) as connection:
        connection.execute("SET LOCAL ROLE p1_migrator")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS agent_business.schema_migrations "
            "(version integer PRIMARY KEY,checksum text NOT NULL)"
        )
        connection.execute("LOCK TABLE agent_business.schema_migrations IN EXCLUSIVE MODE")
        for version, name in (
            (2, "002_durable_runs.sql"),
            (3, "003_run_execution.sql"),
            (4, "004_review_dispatch.sql"),
            (5, "005_usage_quota.sql"),
            (6, "006_authorization_management.sql"),
            (7, "007_migration_release.sql"),
            (8, "008_release_safety.sql"),
        ):
            source = Path("migrations", name).read_text(encoding="utf-8")
            digest = hashlib.sha256(source.encode()).hexdigest()
            previous = connection.execute(
                "SELECT checksum FROM agent_business.schema_migrations WHERE version=%s", (version,)
            ).fetchone()
            if previous:
                if previous[0] != digest:
                    raise RuntimeError("Applied P3 migration checksum changed")
                continue
            if (
                version == 2
                and connection.execute("SELECT to_regclass('agent_business.runs')").fetchone()[0]
            ):
                raise RuntimeError("Unversioned run schema must not be silently adopted")
            connection.execute(source, prepare=False)
            connection.execute(
                "INSERT INTO agent_business.schema_migrations VALUES(%s,%s)", (version, digest)
            )
        # This script is explicitly a disposable test fixture, never a production installer.
        connection.execute("UPDATE agent_business.release_state SET phase='active' WHERE singleton")
    print("P3 durable-run migration ready; P1 runtime remains non-owner and RLS-enforced")


if __name__ == "__main__":
    prepare()
