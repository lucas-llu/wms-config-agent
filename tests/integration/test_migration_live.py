"""P5 offline import and read-only rollback guard in disposable real PG/OIDC."""

import os
import subprocess
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_multiuser_p3_live import p1_system as base_system
from test_multiuser_p3_live import system as base_fixture
from test_multiuser_p3_live import tokens as base_tokens
from test_usage_quota_live import metered, start

from agents.repositories import SessionRepository
from api.users import create_app
from libs.llm import ChatResponse
from multiuser.access import AccessStore
from multiuser.identity import OIDCVerifier
from multiuser.migration import import_snapshot, snapshot
from multiuser.release import prepare_phase, preserve_post_cutover
from multiuser.run_repository import RunRepository
from multiuser.runs import RunLimits
from multiuser.session_auth import TokenIntrospector

p1_system = base_system
system = base_fixture
tokens = base_tokens
pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P5_LIVE") != "1", reason="P5 requires disposable real PG/OIDC"
)


@pytest.fixture
def frozen(system):
    system[-1].execute(
        "UPDATE agent_business.release_state SET phase='frozen',revision=1 WHERE singleton"
    )
    try:
        yield system
    finally:
        system[-1].execute(
            "UPDATE agent_business.release_state SET phase='active',revision=1 WHERE singleton"
        )


def test_explicit_import_is_atomic_idempotent_private_and_never_resumes_checkpoint(
    frozen, tmp_path
):
    system = frozen
    source = SessionRepository(tmp_path / "old.sqlite")
    sid = "session:" + uuid.uuid4().hex
    source.create_session(session_id=sid, goal="PRIVATE synthetic legacy")
    source.append_turn(
        session_id=sid, expected_revision=1, role="user", message="Original private history"
    )
    snapshot(
        source.database_path,
        tmp_path / "bundle",
        writers_stopped=True,
        checkpoints=source.database_path,
    )
    assignments = {
        sid: {
            "user_id": system[2][0].user_id,
            "workspace_id": system[3].workspace_id,
            "reason": "Approved individual owner",
        }
    }
    dsn = os.environ["P0_POSTGRES_DSN"]
    with psycopg.connect(dsn) as connection:
        dry = import_snapshot(connection, tmp_path / "bundle", assignments, system[2][1].user_id)
        assert len(dry["receipts"]) == 1
        assert "Original private history" not in str(dry)
        result = import_snapshot(
            connection, tmp_path / "bundle", assignments, system[2][1].user_id, apply=True
        )
        assert result["imported"] == 1
        assert (
            import_snapshot(
                connection, tmp_path / "bundle", assignments, system[2][1].user_id, apply=True
            )["status"]
            == "already_imported"
        )
        other = {sid: {**assignments[sid], "user_id": system[2][1].user_id}}
        with pytest.raises(ValueError):
            import_snapshot(
                connection, tmp_path / "bundle", other, system[2][1].user_id, apply=True
            )
    owner, other = system[1]
    root = "/v1/conversations/" + sid
    assert (
        system[0].get(root + "/workbench", headers=owner).json()["session"]["legacy_readonly"]
        is True
    )
    assert system[0].get(root + "/workbench", headers=other).status_code == 404
    assert (
        system[-1]
        .execute(
            "SELECT count(*) FROM agent_checkpoints.checkpoints WHERE thread_id IN "
            "(SELECT checkpoint_thread_id FROM agent_business.sessions WHERE session_id=%s)",
            (sid,),
        )
        .fetchone()[0]
        == 0
    )
    system[-1].execute("UPDATE agent_business.release_state SET phase='active' WHERE singleton")
    issuer = os.environ["P0_OIDC_ISSUER"]
    with TestClient(
        create_app(
            OIDCVerifier(issuer, "wms-api", allow_local_http=True),
            TokenIntrospector(issuer, "wms-api", os.environ["P1_API_CLIENT_SECRET"]),
            system[5],
            run_limits=RunLimits(),
            durable_execution=True,
        )
    ) as client:
        assert (
            client.post(
                root + "/runs",
                headers=owner,
                json={
                    "message": "resume old",
                    "expected_revision": 1,
                    "idempotency_key": uuid.uuid4().hex,
                },
            ).status_code
            == 403
        )
    assert source.list_turns(sid)[0].message == "Original private history"


def test_release_freeze_revision_and_readonly_rollback_are_not_runtime_permissions(system):
    admin = system[-1]
    operator = system[2][1].user_id
    admin.execute(
        "UPDATE agent_business.release_state SET phase='active',revision=1 WHERE singleton"
    )
    try:
        with psycopg.connect(os.environ["P0_POSTGRES_DSN"]) as connection:
            assert (
                prepare_phase(connection, "draining", 1, operator, "Approved drain")["applied"]
                is False
            )
            prepare_phase(connection, "draining", 1, operator, "Approved drain", apply=True)
            with pytest.raises(ValueError):
                prepare_phase(connection, "frozen", 1, operator, "Stale state", apply=True)
            # Admission must be refused while reads and existing terminal history remain possible.
            assert (
                system[0]
                .post(
                    "/v1/conversations",
                    headers=system[1][0],
                    json={
                        "workspace_id": system[3].workspace_id,
                        "goal": "Synthetic blocked admission",
                    },
                )
                .status_code
                == 503
            )
            prepare_phase(connection, "frozen", 2, operator, "Completed drain", apply=True)
            prepare_phase(
                connection, "rollback_readonly", 3, operator, "Preserve new data", apply=True
            )
            with pytest.raises(ValueError):
                prepare_phase(connection, "active", 4, operator, "No silent reopen", apply=True)
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            system[4].transaction(system[2][1]) as connection,
        ):
            connection.execute("UPDATE agent_business.release_state SET phase='active'")
    finally:
        admin.execute(
            "UPDATE agent_business.release_state SET phase='active',revision=1 WHERE singleton"
        )


def test_missing_release_metadata_blocks_new_business_writes(system):
    admin = system[-1]
    old = admin.execute("SELECT * FROM agent_business.release_state").fetchone()
    admin.execute("DELETE FROM agent_business.release_state")
    try:
        result = system[0].post(
            "/v1/conversations",
            headers=system[1][0],
            json={"workspace_id": system[3].workspace_id, "goal": "Synthetic must be denied"},
        )
        assert result.status_code == 503
    finally:
        admin.execute("INSERT INTO agent_business.release_state VALUES(%s,%s,%s,%s,%s)", old)


def test_disposable_pg_dump_restore_keeps_post_cutover_usage_and_private_ownership(
    system, tmp_path
):
    """Use the fixture container's matching PG client; never target a real database."""
    with metered(system) as service:
        run = start(system)
        repo = RunRepository(system[4], system[2][0], system[3].workspace_id)
        lease = repo.claim(run["run_id"])
        number = repo.begin_call(lease, "fixture", usage=(service, 200, 100))
        response = ChatResponse(
            "Synthetic returned metadata", metadata={"usage": {"total_tokens": 37}}
        )
        service.record(lease.run_id, "fixture", number, response)
        repo.finish_call(lease, "fixture", number, response)
        repo.cancel(lease.run_id)
        repo.acknowledge_cancel(lease)
        service.reconcile_terminal()
    compose = [
        "docker",
        "compose",
        "--env-file",
        "data/p0-fixture/.env",
        "-f",
        "infra/p0/compose.yml",
    ]

    def container(argv, *, env, stdout, stderr, check):
        fixture_env = {**os.environ, **env, "PGHOST": "127.0.0.1", "PGPORT": "5432"}
        prefix = [*compose, "exec", "-T"]
        for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
            if name in fixture_env:
                prefix += ["-e", name]
        return subprocess.run(
            [*prefix, "postgres", *argv], env=fixture_env, stdout=stdout, stderr=stderr, check=check
        )

    backup = tmp_path / "post-cutover.dump"
    preserved = preserve_post_cutover(
        os.environ["P0_POSTGRES_DSN"], backup, workers_stopped=True, runner=container
    )
    assert preserved["bytes"] > 0 and preserved["legacy_writes_allowed"] is False
    restored_db = "p5_restore_" + uuid.uuid4().hex
    system[-1].execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored_db)))
    with backup.open("rb") as stream:
        result = subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--exit-on-error",
                "--username=postgres",
                "--dbname=" + restored_db,
            ],
            stdin=stream,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    assert result.returncode == 0, "Disposable restore failed; raw diagnostics withheld"
    restored = AccessStore(make_conninfo(os.environ["P1_POSTGRES_DSN"], dbname=restored_db))
    try:
        owner = system[2][0]
        with restored.transaction(owner) as connection:
            assert connection.execute(
                "SELECT used_tokens,held_tokens FROM agent_business.quota_accounts "
                "WHERE user_id=%s",
                (owner.user_id,),
            ).fetchone() == {"used_tokens": 37, "held_tokens": 0}
            assert (
                connection.execute(
                    "SELECT count(*) AS n FROM agent_business.usage_attempts "
                    "WHERE owner_user_id=%s",
                    (owner.user_id,),
                ).fetchone()["n"]
                == 1
            )
            assert connection.execute(
                "SELECT session_id FROM agent_business.sessions WHERE session_id=%s",
                (run["conversation_id"],),
            ).fetchone()
        with restored.transaction(system[2][1]) as connection:
            assert (
                connection.execute(
                    "SELECT session_id FROM agent_business.sessions WHERE session_id=%s",
                    (run["conversation_id"],),
                ).fetchone()
                is None
            )
    finally:
        restored.close()
    # This database belongs solely to this compose project; job teardown destroys its volume.
