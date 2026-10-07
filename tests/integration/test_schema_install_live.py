"""Production-style P5 DDL against fresh databases, never an existing workspace."""

import os
import shutil
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from multiuser.schema_install import inspect_schema, install_schema

pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P5_INSTALL_TEST") != "1",
    reason="Schema installation requires an explicit isolated PostgreSQL test target",
)
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@pytest.fixture
def fresh_database():
    admin_dsn = os.environ["WMS_P5_INSTALL_TEST_DSN"]
    names = []
    with psycopg.connect(admin_dsn, autocommit=True) as server:

        def create():
            name = "wms_p5_schema_" + uuid.uuid4().hex[:16]
            server.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            names.append(name)
            return make_conninfo(admin_dsn, dbname=name), name

        try:
            yield create
        finally:
            for name in names:
                server.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def _secrets():
    return (
        os.environ["WMS_P5_INSTALL_RUNTIME_PASSWORD"],
        os.environ["WMS_P5_INSTALL_CONTROL_PASSWORD"],
    )


def test_fresh_install_is_atomic_frozen_and_repeat_safe(fresh_database, tmp_path):
    broken_dsn, broken_name = fresh_database()
    broken_sources = tmp_path / "broken"
    shutil.copytree(MIGRATIONS, broken_sources)
    with (broken_sources / "008_release_safety.sql").open("a", encoding="utf-8") as output:
        output.write("\nSELECT * FROM agent_business.missing_p5_install_probe;\n")
    with psycopg.connect(broken_dsn, autocommit=True) as target:
        assert inspect_schema(target, broken_sources, broken_name)["state"] == "fresh"
        with pytest.raises(psycopg.errors.UndefinedTable):
            install_schema(target, broken_sources, broken_name, *_secrets())
        assert inspect_schema(target, MIGRATIONS, broken_name)["state"] == "fresh"
        assert target.execute("SELECT to_regclass('agent_business.sessions')").fetchone()[0] is None

    dsn, name = fresh_database()
    with psycopg.connect(dsn, autocommit=True) as target:
        assert inspect_schema(target, MIGRATIONS, name)["pending_versions"] == list(range(1, 9))
        first = install_schema(target, MIGRATIONS, name, *_secrets())
        assert first == {
            "state": "installed",
            "versions": list(range(1, 9)),
            "release_phase": "frozen",
            "release_revision": 1,
            "runtime_roles_non_owner": True,
            "applied": True,
        }
        assert install_schema(target, MIGRATIONS, name, *_secrets())["applied"] is False
        assert (
            target.execute("SELECT count(*) FROM agent_business.schema_migrations").fetchone()[0]
            == 8
        )
        assert target.execute("SELECT count(*) FROM agent_business.sessions").fetchone()[0] == 0
        for role, password in zip(("p1_runtime", "p3_control"), _secrets(), strict=True):
            with psycopg.connect(make_conninfo(dsn, user=role, password=password)) as runtime:
                assert runtime.execute("SELECT current_user").fetchone()[0] == role
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    runtime.execute("UPDATE agent_business.release_state SET phase='active'")

    changed = tmp_path / "changed"
    shutil.copytree(MIGRATIONS, changed)
    with (changed / "003_run_execution.sql").open("a", encoding="utf-8") as output:
        output.write("\n-- deliberately changed after application\n")
    with psycopg.connect(dsn, autocommit=True) as target:
        with pytest.raises(ValueError, match="checksum"):
            inspect_schema(target, changed, name)
        assert inspect_schema(target, MIGRATIONS, name)["release_phase"] == "frozen"


def test_existing_unversioned_schema_is_refused(fresh_database):
    dsn, name = fresh_database()
    with psycopg.connect(dsn, autocommit=True) as target:
        target.execute("CREATE SCHEMA agent_business")
        with pytest.raises(ValueError, match="Unversioned"):
            install_schema(target, MIGRATIONS, name, *_secrets())
        assert (
            target.execute("SELECT to_regclass('agent_business.schema_migrations')").fetchone()[0]
            is None
        )


def test_wrong_target_is_refused_before_any_ddl(fresh_database):
    dsn, name = fresh_database()
    with psycopg.connect(dsn, autocommit=True) as target:
        with pytest.raises(ValueError, match="target database"):
            install_schema(target, MIGRATIONS, "unrelated_db", *_secrets())
        assert inspect_schema(target, MIGRATIONS, name)["state"] == "fresh"
