"""Full P3 graph + current SID + PG governance, no external model or user data."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_multiuser_p3_live import (
    new,
)
from test_multiuser_p3_live import (
    p1_system as base_system,
)
from test_multiuser_p3_live import (
    system as system_fixture,
)
from test_multiuser_p3_live import (
    tokens as tokens_fixture,
)

from api.users import create_app
from core.settings import load_settings
from multiuser.agent import UserAgent
from multiuser.control import RunControl
from multiuser.executor import RunExecutor
from multiuser.governor import ModelBusy, ModelGovernor, ModelLimits
from multiuser.identity import OIDCVerifier
from multiuser.queued_application import start_run
from multiuser.run_repository import RunRepository
from multiuser.runs import RunLimits, RunRequest
from multiuser.session_auth import TokenIntrospector
from multiuser.session_authority import SessionAuthority
from scripts.p2_fixture_app import SyntheticKnowledge, SyntheticModel

p1_system = base_system
system = system_fixture
tokens = tokens_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P3_LIVE") != "1", reason="P3 execution needs disposable services"
)


@contextmanager
def executor(system, *, model=None, limits=None, capacity=4):
    control = RunControl(os.environ["P3_CONTROL_DSN"], limits=limits)
    authority = SessionAuthority(
        os.environ["P0_OIDC_ISSUER"],
        "wms-session-reader",
        os.environ["P3_SESSION_CLIENT_SECRET"],
        allow_local_http=True,
    )
    governor = ModelGovernor(
        control,
        "test:" + uuid.uuid4().hex,
        limits=ModelLimits(inflight=2, review_inflight=1, tpm=1000000),
    )
    agent = UserAgent(
        system[4],
        os.environ["P1_POSTGRES_DSN"],
        model or SyntheticModel(),
        load_settings().agent,
        SyntheticKnowledge(),
    )
    try:
        retrieval = ModelGovernor(
            control,
            "retrieval:" + uuid.uuid4().hex,
            limits=ModelLimits(inflight=1, review_inflight=0, tpm=1000000),
        )
        yield RunExecutor(
            system[4],
            control,
            authority,
            governor,
            agent,
            capacity=capacity,
            retrieval_governor=retrieval,
        )
    finally:
        authority.close()
        control.close()


def accepted(system, *, message="SYN_MODE 是什么？", strategy="standard"):
    repo = RunRepository(system[4], system[2][0], system[3].workspace_id)
    key = uuid.uuid4().hex
    run = start_run(
        system[5],
        system[2][0],
        goal=message,
        workspace_id=system[3].workspace_id,
        answer_strategy=strategy,
        idempotency_key=key,
        limits=RunLimits(),
    )
    return repo, run, key


def test_complete_graph_private_first_message_followup_and_duplicate_delivery(system):
    repo, run, key = accepted(system)
    with executor(system) as service:
        result = service.execute(run["run_id"])
        assert result == {"accepted": True, "committed": True}, result
        saved = repo.sessions.get_session(run["conversation_id"])
        assert saved.current_revision == 2
        turns = repo.sessions.list_turns(saved.session_id)
        assert [t.role for t in turns] == ["user", "assistant"]
        assert "SYN_MODE" in turns[-1].message
        assert turns[-1].metadata["citations"]
        assert not service.execute(run["run_id"])["accepted"]
        assert len(repo.sessions.list_turns(saved.session_id)) == 2
        again = start_run(
            system[5],
            system[2][0],
            goal="SYN_MODE 是什么？",
            workspace_id=system[3].workspace_id,
            answer_strategy="standard",
            idempotency_key=key,
            limits=RunLimits(),
        )
        assert again["run_id"] == run["run_id"]
        follow = repo.submit(
            saved.session_id, RunRequest("这个选项是可选的吗？", uuid.uuid4().hex, 2)
        )
        assert service.execute(follow["run_id"])["committed"]
        assert len(repo.sessions.list_turns(saved.session_id)) == 4
        assert repo.sessions.get_session(saved.session_id).current_revision == 3


def test_metadata_control_role_has_no_private_body_or_checkpoint_access(system):
    _, run, _ = accepted(system)
    with RunControl(os.environ["P3_CONTROL_DSN"]).pool.connection() as connection:
        assert connection.execute(
            "SELECT run_id FROM agent_business.runs WHERE run_id=%s", (run["run_id"],)
        ).fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT message FROM agent_business.runs")
        connection.rollback()
        for query in (
            "SELECT * FROM agent_business.sessions",
            "SELECT * FROM agent_checkpoints.checkpoints",
            "SELECT * FROM agent_business.run_model_calls",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(query)
            connection.rollback()
    with pytest.raises(ValueError):
        RunControl(os.environ["P1_POSTGRES_DSN"])


def test_fair_owner_dispatch_and_ack_only_contains_opaque_ids(system):
    repo, run, _ = accepted(system)
    repo.submit(new(system), RunRequest("second queued", uuid.uuid4().hex, 1))
    other = RunRepository(system[4], system[2][1], system[3].workspace_id)
    other_run = other.submit(new(system, 1), RunRequest("other", uuid.uuid4().hex, 1))
    with executor(system) as service:
        offers = service.control.offers()
        assert {o["run_id"] for o in offers} == {run["run_id"], other_run["run_id"]}
        assert all(set(o) == {"run_id", "delivery_id"} for o in offers)
        for offer in offers:
            service.control.dispatched(**offer)
        assert (
            len(service.control.offers()) == 1
        )  # owner's next conversation, not same queued head.


def test_global_permits_review_subcap_rpm_tpm_and_restart_do_not_reset(system):
    with executor(system) as service:
        gov = service.governor
        with ThreadPoolExecutor(max_workers=5) as pool:
            permits = list(pool.map(lambda _: gov.reserve(100), range(10)))
        assert len([p for p in permits if p]) == 2
        assert gov.reserve(100) is None
        for permit in permits:
            if permit:
                gov.release(permit, actual=10)
        first = gov.reserve(100, review=True)
        assert first
        assert gov.reserve(100, review=True) is None
        assert gov.reserve(100, review=False)
        reopened = ModelGovernor(service.control, gov.key, limits=gov.limits)
        assert reopened.reserve(1) is None
        with pytest.raises(ValueError):
            ModelGovernor(service.control, gov.key, limits=ModelLimits(inflight=3))
        with pytest.raises(ModelBusy):
            gov.reserve(1000001)
        tight = ModelGovernor(
            service.control, "tight:" + uuid.uuid4().hex, limits=ModelLimits(rpm=1, tpm=100)
        )
        permit = tight.reserve(100)
        tight.release(permit)
        assert tight.reserve(1) is None  # releasing in-flight does NOT reset the one-minute window.
        reading = service.retrieval_governor.reserve(1)
        assert reading  # Independent from the exhausted model permits, still globally bounded.
        assert service.retrieval_governor.reserve(1) is None
        service.retrieval_governor.release(reading)


def test_cancel_during_model_does_not_publish_partial_user_or_late_answer(system):
    repo, run, _ = accepted(system)
    delegate = SyntheticModel()

    class CancelModel:
        def chat(self, messages, trace=None):
            repo.cancel(run["run_id"])
            return delegate.chat(messages, trace)

    with executor(system, model=CancelModel()) as service:
        assert service.execute(run["run_id"])["reason"] == "lost_lease"
        assert repo.get(run["run_id"])["status"] == "cancelled"
        assert repo.sessions.list_turns(run["conversation_id"]) == ()
        assert repo.sessions.get_session(run["conversation_id"]).current_revision == 1


def test_real_authority_device_revocation_blocks_execution(system):
    repo, run, _ = accepted(system)
    with executor(system) as service:
        # App-side durable revocation is independent from JWT expiry and checks before claim.
        system[4].revoke(system[2][0])
        try:
            assert service.execute(run["run_id"])["reason"] == "authorization_required"
        finally:
            system[-1].execute(
                "DELETE FROM identity_business.revoked_sessions WHERE user_id=%s AND sid=%s",
                (system[2][0].user_id, system[2][0].sid),
            )
        assert repo.get(run["run_id"])["status"] == "authorization_required"


def test_async_http_contract_first_202_lookup_and_sync_bypass_rejected(system):
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
        headers = system[1][0]
        assert client.get("/v1/me", headers=headers).json()["durable_runs"] is True
        data = {
            "goal": "SYN_MODE?",
            "workspace_id": system[3].workspace_id,
            "idempotency_key": uuid.uuid4().hex,
        }
        first = client.post("/v1/conversations", headers=headers, json=data)
        assert first.status_code == 202, first.text
        run = first.json()
        assert client.post("/v1/conversations", headers=headers, json=data).json() == run
        lookup = (
            "/v1/runs/lookup?workspace_id="
            + system[3].workspace_id
            + "&idempotency_key="
            + data["idempotency_key"]
        )
        assert client.get(lookup, headers=headers).json() == run
        assert client.get(lookup, headers=system[1][1]).status_code == 404
        request_path = "/v1/runs/" + run["run_id"] + "/request"
        assert client.get(request_path, headers=headers).json() == {"message": "SYN_MODE?"}
        assert client.get(request_path, headers=system[1][1]).status_code == 404
        assert (
            client.post(
                "/v1/conversations",
                headers=headers,
                json={"goal": "q", "workspace_id": system[3].workspace_id},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/conversations/" + run["conversation_id"] + "/continue",
                headers=headers,
                json={"message": "q", "expected_revision": 1},
            ).status_code
            == 409
        )
