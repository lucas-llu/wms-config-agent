"""P5 offline import and read-only rollback guard in disposable real PG/OIDC."""

import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_multiuser_p3_live import p1_system as base_system
from test_multiuser_p3_live import system as base_fixture
from test_multiuser_p3_live import tokens as base_tokens

from agents.repositories import SessionRepository
from api.users import create_app
from multiuser.identity import OIDCVerifier
from multiuser.migration import import_snapshot, snapshot
from multiuser.release import prepare_phase
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
