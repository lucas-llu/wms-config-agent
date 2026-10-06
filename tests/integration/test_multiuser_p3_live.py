"""P3 storage invariants on real PG RLS roles; never call a paid provider."""

import asyncio
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_multiuser_p1_live import new
from test_multiuser_p1_live import system as system_fixture
from test_multiuser_p1_live import tokens as tokens_fixture

from agents.repositories import SessionNotFoundError, SessionRevisionConflict
from agents.runtime import build_runtime_probe_graph
from api.users import create_app
from core.settings import load_settings
from multiuser.access import AccessDenied
from multiuser.agent import UserAgent
from multiuser.identity import OIDCVerifier
from multiuser.run_repository import RunRepository
from multiuser.runs import LostLease, RunBusy, RunConflict, RunLimits, RunRequest
from multiuser.session_auth import TokenIntrospector

p1_system = system_fixture
tokens = tokens_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P3_LIVE") != "1", reason="P3 requires an explicitly isolated PostgreSQL fixture"
)


@pytest.fixture
def system(p1_system):
    yield p1_system
    # Only this fixture's synthetic workspace; don't leak tasks into next test's cap.
    p1_system[-1].execute(
        "DELETE FROM agent_business.runs r USING agent_business.sessions s "
        "WHERE r.conversation_id=s.session_id AND s.workspace_id=%s",
        (p1_system[3].workspace_id,),
    )


def repository(system, index=0, limits=None):
    _, _, contexts, workspace, store, _, _ = system
    return RunRepository(store, contexts[index], workspace.workspace_id, limits=limits)


def request(**change):
    return replace(RunRequest("Synthetic private follow-up", uuid.uuid4().hex, 1), **change)


def submit(system, repo=None, **change):
    repo = repo or repository(system)
    body = request(**change)
    conversation = new(system)
    return repo, conversation, body, repo.submit(conversation, body)


def expire(system, run_id):
    system[-1].execute(
        "UPDATE agent_business.runs SET lease_until=clock_timestamp()-interval '1 second' "
        "WHERE run_id=%s",
        (run_id,),
    )


def persist(system, conversation, connection):
    # Synthetic trusted persistence, intentionally on the provided SHORT transaction.
    initial = connection.execute(
        "SELECT * FROM agent_business.revisions WHERE session_id=%s AND revision=1",
        (conversation,),
    ).fetchone()
    connection.execute(
        "INSERT INTO agent_business.revisions SELECT session_id,2,status,state_json,"
        "state_fingerprint,actor,reason,created_at FROM agent_business.revisions "
        "WHERE session_id=%s AND revision=1",
        (conversation,),
    )
    connection.execute(
        "UPDATE agent_business.sessions SET current_revision=2 WHERE session_id=%s", (conversation,)
    )
    connection.execute(
        "INSERT INTO agent_business.turns VALUES(%s,%s,2,1,'assistant','Verified fixture','{}',%s)",
        (uuid.uuid4().hex, conversation, initial["created_at"]),
    )
    return 2


def test_idempotent_multitab_submit_and_private_parent(system):
    repo, conversation, body, accepted = submit(system)
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: repo.submit(conversation, body), range(10)))
    assert {r["run_id"] for r in results} == {accepted["run_id"]}
    assert len(repo.pending()) == 1
    for change in ({"message": "other"}, {"answer_strategy": "review"}, {"expected_revision": 2}):
        with pytest.raises(RunConflict, match="idempotency_conflict"):
            repo.submit(conversation, replace(body, **change))
    with pytest.raises(RunBusy, match="conversation_busy"):
        repo.submit(conversation, request())
    other = repository(system, 1)
    for operation in (
        lambda: other.get(accepted["run_id"]),
        lambda: other.cancel(accepted["run_id"]),
        lambda: other.events(accepted["run_id"]),
        lambda: other.claim(accepted["run_id"]),
        lambda: other.submit(conversation, body),
        lambda: other.active(conversation),
    ):
        with pytest.raises(SessionNotFoundError):
            operation()
    assert other.pending() == []
    assert other.recover() == 0
    assert repo.active(conversation)["run_id"] == accepted["run_id"]


def test_admission_and_claim_caps_are_atomic(system):
    repo = repository(system, limits=RunLimits(pending_per_user=2, running_per_user=1))
    _, _, _, first = submit(system, repo)
    _, _, _, second = submit(system, repo)
    with pytest.raises(RunBusy, match="user_queue_full"):
        submit(system, repo)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(repo.claim, [first["run_id"], second["run_id"]]))
    assert sum(c is not None for c in claims) == 1
    running = first if claims[0] else second
    waiting = second if claims[0] else first
    assert repo.claim(running["run_id"]) is None
    assert repo.claim(waiting["run_id"]) is None
    assert repo.cancel(running["run_id"])["status"] == "cancelling"
    assert repo.acknowledge_cancel(next(c for c in claims if c))
    assert repo.claim(waiting["run_id"]) is not None


def test_completion_is_atomic_and_idempotent_after_revision_advances(system):
    repo, conversation, body, accepted = submit(system)
    lease = repo.claim(accepted["run_id"])
    repo.heartbeat(lease)
    repo.progress(lease, "generating")
    repo.progress(lease, "generating")  # No duplicate stage event.
    repo.model_started(lease)
    with pytest.raises(RunConflict, match="model_attempt_open"):
        repo.complete(lease, lambda conn: persist(system, conversation, conn))
    repo.model_finished(lease)
    with pytest.raises(RunConflict, match="no_model_attempt"):
        repo.model_finished(lease)
    repo.progress(lease, "validating")
    repo.progress(lease, "persisting")
    completed = repo.complete(lease, lambda conn: persist(system, conversation, conn))
    assert completed["status"] == "succeeded"
    assert completed["result_revision"] == 2
    assert repo.submit(conversation, body)["run_id"] == lease.run_id
    assert repo.active(conversation) is None
    with pytest.raises(LostLease):
        repo.complete(lease, lambda conn: pytest.fail("Must never call a stale writer"))
    events = repo.events(lease.run_id)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["result_revision"] == 2
    assert repo.events(lease.run_id, after=events[-2]["sequence"], limit=1) == events[-1:]
    assert repo.events(lease.run_id, after=events[-1]["sequence"]) == []


@pytest.mark.parametrize("failure", ["raise", "invalid_revision", "missing_answer"])
def test_failed_persistence_rolls_back_every_write(system, failure):
    repo, conversation, _, accepted = submit(system)
    lease = repo.claim(accepted["run_id"])

    def invalid(connection):
        if failure == "missing_answer":
            return 2
        persist(system, conversation, connection)
        if failure == "raise":
            raise ValueError("Synthetic persistence error")
        return 1

    with pytest.raises(ValueError):
        repo.complete(lease, invalid)
    assert repo.get(lease.run_id)["status"] == "running"
    assert (
        system[5].repository(system[2][0], conversation).get_session(conversation).current_revision
        == 1
    )
    assert (
        system[-1]
        .execute("SELECT count(*) FROM agent_business.turns WHERE session_id=%s", (conversation,))
        .fetchone()[0]
        == 0
    )


def test_queued_cancel_running_cancel_and_late_result_fencing(system):
    repo, conversation, _, accepted = submit(system)
    assert repo.cancel(accepted["run_id"])["status"] == "cancelled"
    assert repo.claim(accepted["run_id"]) is None
    assert repo.cancel(accepted["run_id"])["status"] == "cancelled"
    assert repo.pending() == []
    later = repo.submit(conversation, request())
    lease = repo.claim(later["run_id"])
    repo.model_started(lease)
    assert repo.cancel(lease.run_id)["status"] == "cancelling"
    for operation in (
        lambda: repo.heartbeat(lease),
        lambda: repo.model_finished(lease),
        lambda: repo.complete(lease, lambda conn: pytest.fail("Late answer")),
    ):
        with pytest.raises(LostLease):
            operation()
    assert not repo.acknowledge_cancel(replace(lease, epoch=lease.epoch + 1))
    assert repo.acknowledge_cancel(lease)
    assert not repo.acknowledge_cancel(lease)


def test_lost_outbox_ack_replay_and_pre_model_recovery(system):
    repo, _, _, accepted = submit(system)
    delivery = repo.pending()[0]
    assert repo.dispatched(**delivery)
    assert not repo.dispatched(**delivery)
    lease = repo.claim(accepted["run_id"])
    expire(system, lease.run_id)
    with pytest.raises(LostLease):
        repo.heartbeat(lease)
    assert repo.recover() == 1
    replacement = repo.pending()[0]
    assert replacement["delivery_id"] != delivery["delivery_id"]
    assert not repo.dispatched(**delivery)
    next_lease = repo.claim(lease.run_id)
    assert next_lease.epoch > lease.epoch
    assert next_lease.checkpoint_thread != lease.checkpoint_thread
    with pytest.raises(LostLease):
        repo.complete(lease, lambda conn: pytest.fail("Stale epoch"))
    assert repo.recover() == 0


def test_ambiguous_provider_attempt_never_auto_replays(system):
    repo, _, _, accepted = submit(system)
    lease = repo.claim(accepted["run_id"])
    repo.model_started(lease)
    expire(system, lease.run_id)
    assert repo.recover() == 1
    assert repo.get(lease.run_id)["status"] == "uncertain"
    assert repo.pending() == []
    assert repo.claim(lease.run_id) is None


def test_known_model_result_needs_checkpoint_takeover_not_restart(system):
    repo, _, _, accepted = submit(system)
    lease = repo.claim(accepted["run_id"])
    repo.model_started(lease)
    repo.model_finished(lease)
    expire(system, lease.run_id)
    assert repo.recover() == 1
    assert repo.get(lease.run_id)["status"] == "recovery_required"
    assert repo.pending() == []
    assert repo.claim(lease.run_id) is None
    with pytest.raises(LostLease):
        repo.heartbeat(lease)


def test_execution_deadline_independently_fences_and_recovers(system):
    repo = repository(system, limits=RunLimits(lease_seconds=120, execution_seconds=5))
    _, _, _, accepted = submit(system, repo)
    lease = repo.claim(accepted["run_id"])
    bounds = (
        system[-1]
        .execute(
            "SELECT lease_until<=execution_deadline FROM agent_business.runs WHERE run_id=%s",
            (lease.run_id,),
        )
        .fetchone()
    )
    assert bounds[0]
    system[-1].execute(
        "UPDATE agent_business.runs SET execution_deadline=clock_timestamp()-interval '1 second' "
        "WHERE run_id=%s",
        (lease.run_id,),
    )
    with pytest.raises(LostLease):
        repo.heartbeat(lease)
    assert repo.recover() == 1
    assert repo.get(lease.run_id)["status"] == "queued"


def test_forged_lease_and_changed_revision_cannot_commit(system):
    repo, conversation, _, accepted = submit(system)
    lease = repo.claim(accepted["run_id"])
    for changed in (
        {"epoch": lease.epoch + 1},
        {"conversation_id": "other"},
        {"checkpoint_thread": "caller-owned-thread"},
    ):
        with pytest.raises(LostLease):
            repo.heartbeat(replace(lease, **changed))
    system[-1].execute(
        "UPDATE agent_business.sessions SET current_revision=2 WHERE session_id=%s", (conversation,)
    )
    with pytest.raises(RunConflict, match="conversation_changed"):
        repo.complete(lease, lambda conn: pytest.fail("Changed baseline"))


def test_account_and_request_context_revocation_fail_closed(system):
    repo, _, _, accepted = submit(system)
    lease = repo.claim(accepted["run_id"])
    context = system[2][0]
    admin = system[-1]
    admin.execute(
        "UPDATE identity_business.users SET status='disabled' WHERE user_id=%s", (context.user_id,)
    )
    try:
        with pytest.raises(AccessDenied):
            repo.heartbeat(lease)
    finally:
        admin.execute(
            "UPDATE identity_business.users SET status='active' WHERE user_id=%s",
            (context.user_id,),
        )
    with pytest.raises(AccessDenied):
        RunRepository(system[4], replace(context, expires_at=1), system[3].workspace_id)
    system[4].revoke(context)
    try:
        with pytest.raises(AccessDenied):
            repo.get(accepted["run_id"])
    finally:
        admin.execute(
            "DELETE FROM identity_business.revoked_sessions WHERE user_id=%s AND sid=%s",
            (context.user_id, context.sid),
        )


def test_purge_removes_real_checkpoint_generations_not_other_conversations(system):
    repo, conversation, _, accepted = submit(system)
    lease = repo.claim(accepted["run_id"])
    other_conversation = new(system)
    context = system[2][0]
    agent = UserAgent(system[4], os.environ["P1_POSTGRES_DSN"], None, load_settings().agent)

    async def save():
        async with agent.saver(context) as saver:
            graph = build_runtime_probe_graph(saver)
            for thread in (lease.checkpoint_thread, other_conversation):
                await graph.ainvoke(
                    {"subject": "synthetic-private"}, {"configurable": {"thread_id": thread}}
                )

    asyncio.run(save())
    session_repo = system[5].repository(context, conversation)
    session_repo.delete_session(conversation)
    assert session_repo.purge_deleted_sessions({conversation: 1}) == 1
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert (
            system[-1]
            .execute(
                f"SELECT count(*) FROM agent_checkpoints.{table} WHERE thread_id=%s",
                (lease.checkpoint_thread,),
            )
            .fetchone()[0]
            == 0
        )
    assert (
        system[-1]
        .execute(
            "SELECT count(*) FROM agent_checkpoints.checkpoints WHERE thread_id=%s",
            (other_conversation,),
        )
        .fetchone()[0]
        > 0
    )


def test_recovery_bounded_cancel_timeout_and_delivery_ceiling(system):
    repo = repository(system, limits=RunLimits(max_deliveries=1))
    _, _, _, failed = submit(system, repo)
    delivery = repo.pending()[0]
    repo.dispatched(**delivery)
    lease = repo.claim(failed["run_id"])
    expire(system, lease.run_id)
    assert repo.recover(limit=1) == 1
    assert repo.get(lease.run_id)["status"] == "failed"
    _, _, _, cancelled = submit(system, repo)
    lease = repo.claim(cancelled["run_id"])
    repo.cancel(lease.run_id)
    expire(system, lease.run_id)
    assert repo.recover() == 1
    assert repo.get(lease.run_id)["status"] == "cancelled"
    _, _, _, queued = submit(system, repo)
    # Queue deadline is immutable even for table owner; use a bound clock delay instead.
    system[-1].execute("ALTER TABLE agent_business.runs DISABLE TRIGGER immutable_run_binding")
    try:
        system[-1].execute(
            "UPDATE agent_business.runs SET queue_deadline=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s",
            (queued["run_id"],),
        )
    finally:
        system[-1].execute("ALTER TABLE agent_business.runs ENABLE TRIGGER immutable_run_binding")
    assert repo.claim(queued["run_id"]) is None
    assert repo.get(queued["run_id"])["status"] == "timed_out"


def test_revocation_deletion_and_revision_changes_block_execution(system):
    repo, conversation, _, accepted = submit(system)
    lease = repo.claim(accepted["run_id"])
    admin = system[-1]
    admin.execute(
        "UPDATE identity_business.memberships SET active=false "
        "WHERE user_id=%s AND workspace_id=%s",
        (system[2][0].user_id, system[3].workspace_id),
    )
    with pytest.raises((AccessDenied, SessionNotFoundError)):
        repo.heartbeat(lease)
    admin.execute(
        "UPDATE identity_business.memberships SET active=true WHERE user_id=%s AND workspace_id=%s",
        (system[2][0].user_id, system[3].workspace_id),
    )
    system[5].repository(system[2][0], conversation).delete_session(conversation)
    with pytest.raises(SessionNotFoundError):
        repo.get(accepted["run_id"])
    with pytest.raises(SessionNotFoundError):
        repo.heartbeat(lease)
    repo, conversation, _, accepted = submit(system)
    with pytest.raises(SessionRevisionConflict):
        repo.submit(conversation, request(expected_revision=2))
    admin.execute(
        "UPDATE agent_business.sessions SET current_revision=2 WHERE session_id=%s", (conversation,)
    )
    assert repo.claim(accepted["run_id"]) is None
    assert repo.get(accepted["run_id"])["status"] == "failed"


@pytest.mark.parametrize("method", ["events", "pending", "recover"])
def test_invalid_limits_and_missing_rows(system, method):
    repo, _, _, accepted = submit(system)
    operation = getattr(repo, method)
    for value in (0, 101, True, -1):
        with pytest.raises(ValueError):
            operation(accepted["run_id"], limit=value) if method == "events" else operation(
                limit=value
            )
    for value in (-1, True, 1.0):
        with pytest.raises(ValueError):
            repo.events(accepted["run_id"], after=value)
    with pytest.raises(RunConflict, match="event_cursor_ahead"):
        repo.events(accepted["run_id"], after=100)
    with pytest.raises(ValueError):
        repo.progress(repo.claim(accepted["run_id"]), "raw provider output")
    with pytest.raises(TypeError):
        repo.heartbeat({"epoch": 1})
    with pytest.raises(TypeError):
        repo.submit("missing", {})


@contextmanager
def run_client(system):
    issuer = os.environ["P0_OIDC_ISSUER"]
    verifier = OIDCVerifier(issuer, "wms-api", allow_local_http=True)
    intro = TokenIntrospector(issuer, "wms-api", os.environ["P1_API_CLIENT_SECRET"])
    with TestClient(create_app(verifier, intro, system[5], run_limits=RunLimits())) as client:
        yield client


def test_real_http_runs_and_sse_reconnect_are_owned_and_nonexecuting(system):
    repo, conversation, body, _ = submit(system)
    # Cancel the direct task first; then exercise the HTTP acceptance and idempotent replay.
    repo.cancel(repo.active(conversation)["run_id"])
    with run_client(system) as client:
        root = "/v1/conversations/" + conversation
        headers = system[1][0]
        data = {
            "message": body.message,
            "idempotency_key": uuid.uuid4().hex,
            "expected_revision": 1,
            "answer_strategy": "standard",
        }
        accepted = client.post(root + "/runs", headers=headers, json=data)
        assert accepted.status_code == 202, accepted.text
        run = accepted.json()
        run_root = "/v1/runs/" + run["run_id"]
        assert client.post(root + "/runs", headers=headers, json=data).json() == run
        assert client.get(root + "/runs/active", headers=headers).json() == run
        assert client.get(run_root, headers=headers).json() == run
        assert client.get(run_root).status_code == 401
        for key in ("owner_user_id", "thread_id", "checkpoint_ns", "roles"):
            assert (
                client.post(
                    root + "/runs", headers=headers, json={**data, key: "untrusted"}
                ).status_code
                == 422
            )
        assert (
            client.post(
                root + "/runs", headers=headers, json={**data, "message": "different"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                root + "/runs", headers=headers, json={**data, "idempotency_key": uuid.uuid4().hex}
            ).status_code
            == 409
        )
        for path, method in (
            (run_root, "GET"),
            (run_root + "/events", "GET"),
            (run_root + "/cancel", "POST"),
            (root + "/runs/active", "GET"),
        ):
            assert client.request(method, path, headers=system[1][1]).status_code == 404
        assert (
            client.get(
                run_root + "/events", headers={**headers, "Last-Event-ID": "other/1"}
            ).status_code
            == 422
        )
        assert (
            client.get(
                run_root + "/events", headers={**headers, "Last-Event-ID": run["run_id"] + "/999"}
            ).status_code
            == 409
        )
        assert client.post(run_root + "/cancel", headers=headers).json()["status"] == "cancelled"
        stream = client.get(run_root + "/events", headers=headers)
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert "event: progress" in stream.text and "cancelled" in stream.text
        assert "Synthetic private" not in stream.text
        reconnected = client.get(
            run_root + "/events", headers={**headers, "Last-Event-ID": run["run_id"] + "/1"}
        )
        assert "/1\n" not in reconnected.text and "/2\n" in reconnected.text
        final = client.get(
            run_root + "/events", headers={**headers, "Last-Event-ID": run["run_id"] + "/2"}
        )
        assert final.text == ""
        assert repo.get(run["run_id"])["sequence"] == 2


def test_lease_expiring_during_persistence_rolls_back(system):
    repo, conversation, _, accepted = submit(system)
    lease = repo.claim(accepted["run_id"])
    # Fixture admin places the bound lease just before its deadline; callback has no network I/O.
    system[-1].execute(
        "UPDATE agent_business.runs SET lease_until=clock_timestamp()+interval '0.1 second' "
        "WHERE run_id=%s",
        (lease.run_id,),
    )

    def too_late(connection):
        revision = persist(system, conversation, connection)
        time.sleep(0.15)
        return revision

    with pytest.raises(LostLease):
        repo.complete(lease, too_late)
    assert (
        system[5].repository(system[2][0], conversation).get_session(conversation).current_revision
        == 1
    )
