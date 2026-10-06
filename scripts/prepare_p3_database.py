"""Versioned P3 migration for the disposable CI fixture (not public startup DDL)."""

import hashlib
import os
from pathlib import Path

import psycopg


def prepare():
    if os.getenv("WMS_P3_LIVE") != "1":
        raise RuntimeError("P3 fixture migration requires explicit isolated-test mode")
    source = Path("migrations/002_durable_runs.sql").read_text(encoding="utf-8")
    digest = hashlib.sha256(source.encode()).hexdigest()
    with psycopg.connect(os.environ["P0_POSTGRES_DSN"]) as connection:
        connection.execute("SET LOCAL ROLE p1_migrator")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS agent_business.schema_migrations "
            "(version integer PRIMARY KEY,checksum text NOT NULL)"
        )
        connection.execute("LOCK TABLE agent_business.schema_migrations IN EXCLUSIVE MODE")
        previous = connection.execute(
            "SELECT checksum FROM agent_business.schema_migrations WHERE version=2"
        ).fetchone()
        if previous:
            if previous[0] != digest:
                raise RuntimeError("Applied P3 migration checksum changed")
            return
        if connection.execute("SELECT to_regclass('agent_business.runs')").fetchone()[0]:
            raise RuntimeError("Unversioned run schema must not be silently adopted")
        connection.execute(source, prepare=False)
        connection.execute("INSERT INTO agent_business.schema_migrations VALUES(2,%s)", (digest,))
    print("P3 durable-run migration ready; P1 runtime remains non-owner and RLS-enforced")


if __name__ == "__main__":
    prepare()
