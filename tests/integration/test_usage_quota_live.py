"""Real PG/RLS token accounting, no paid provider or private deployment data."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_multiuser_p3_live import p1_system as base_system
from test_multiuser_p3_live import system as system_fixture
from test_multiuser_p3_live import tokens as tokens_fixture
from test_run_execution_live import executor

from api.users import create_app
from libs.llm import ChatResponse
from multiuser.access import AccessDenied
from multiuser.control import RunControl
from multiuser.identity import OIDCVerifier
from multiuser.metering import Usage
from multiuser.queued_application import start_run
from multiuser.run_repository import RunRepository
from multiuser.runs import RunConflict, RunLimits
from multiuser.session_auth import TokenIntrospector
from multiuser.session_repository import PostgresSessionRepository
from multiuser.usage import QuotaExceeded, UsageService

p1_system = base_system
system = system_fixture
tokens = tokens_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P4_LIVE") != "1", reason="P4 requires disposable real PG/OIDC"
)
PRICE = {
    "version": "fixture-v1",
    "currency": "USD",
    "input": "2",
    "cached": "0.5",
    "output": "4",
    "confirmed": True,
}


@contextmanager
def metered(system, *, limit=1000, budget=300):
    control = RunControl(os.environ["P3_CONTROL_DSN"])
    service = UsageService(
        monthly_tokens=limit,
        run_budget=budget,
        model="synthetic",
        provider_key="fixture",
        price=PRICE,
        control=control,
    )
    store = system[4]
    store.usage = service
    # Shared synthetic identity across tests, reset only these fixture user quota limits.
    for table in ("usage_attempts", "quota_reservations", "usage_runs"):
        system[-1].execute(
            f"DELETE FROM agent_business.{table} WHERE owner_user_id=%s", (system[2][0].user_id,)
        )
    system[-1].execute(
        "DELETE FROM agent_business.quota_accounts WHERE user_id=%s", (system[2][0].user_id,)
    )
    system[-1].execute(
        "DELETE FROM agent_business.quota_limits WHERE user_id=%s", (system[2][0].user_id,)
    )
    try:
        yield service
    finally:
        store.usage = None
        control.close()


def start(system, key=None):
    return start_run(
        system[5],
        system[2][0],
        goal="Synthetic metering request",
        workspace_id=system[3].workspace_id,
        answer_strategy="standard",
        idempotency_key=key or uuid.uuid4().hex,
        limits=RunLimits(),
    )


def test_atomic_concurrent_admission_and_idempotency_do_not_double_reserve(system):
    with metered(system, limit=600, budget=300) as service:
        key = uuid.uuid4().hex
        first = start(system, key)
        assert start(system, key)["run_id"] == first["run_id"]

        def submit(_):
            try:
                return start(system)
            except QuotaExceeded:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
        assert sum(r is not None for r in results) == 1
        summary = service.summary(system[4], system[2][0])
        assert summary["reserved"] == 600 and summary["remaining"] == 0
        assert summary["month_requests"] == 2


def test_attempt_retries_cache_idempotency_unknown_and_late_result_settlement(system):
    with metered(system) as service:
        run = start(system)
        repo = RunRepository(system[4], system[2][0], system[3].workspace_id)
        lease = repo.claim(run["run_id"])
        attempt = repo.begin_call(lease, "call", usage=(service, 200, 120))
        service.record(lease.run_id, "call", attempt, rejected=True)
        repo.finish_call(lease, "call", attempt, rejected=True)
        attempt = repo.begin_call(lease, "call", usage=(service, 200, 120))
        repo.cancel(lease.run_id)
        repo.acknowledge_cancel(lease)
        service.reconcile_terminal()
        assert service.summary(system[4], system[2][0])["reserved"] == 200
        response = ChatResponse(
            "not published",
            model="synthetic",
            metadata={
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "prompt_cache_hit_tokens": 40,
                },
                "provider_request_id": "fixture-request",
            },
        )
        service.record(lease.run_id, "call", attempt, response)
        service.record(lease.run_id, "call", attempt, response)
        summary = service.summary(system[4], system[2][0])
        assert summary["used"] == 150 and summary["reserved"] == 0 and summary["attempts"] == 2
        assert summary["costs"] == [{"currency": "USD", "amount": "0.000340000000"}]
        assert repo.sessions.list_turns(run["conversation_id"]) == ()
        repo.sessions.delete_session(run["conversation_id"])
        repo.sessions.purge_deleted_sessions({run["conversation_id"]: 1})
        assert service.summary(system[4], system[2][0])["used"] == 150
        assert len(service.details(system[4], system[2][0])) == 2


def test_missing_usage_estimates_but_unknown_timeout_keeps_hold(system):
    with metered(system) as service:
        run = start(system)
        repo = RunRepository(system[4], system[2][0], system[3].workspace_id)
        lease = repo.claim(run["run_id"])
        n = repo.begin_call(lease, "known", usage=(service, 200, 100))
        service.record(lease.run_id, "known", n, ChatResponse("abc"))
        repo.finish_call(lease, "known", n, ChatResponse("abc"))
        repo.begin_call(lease, "unknown", usage=(service, 100, 50))
        repo.cancel(lease.run_id)
        repo.acknowledge_cancel(lease)
        service.reconcile_terminal()
        summary = service.summary(system[4], system[2][0])
        assert summary["used"] == 103 and summary["reserved"] == 100
        assert summary["unknown_attempts"] == 1 and summary["estimated_attempts"] == 1


def test_api_private_usage_admin_quotas_and_reconciliation_are_audited(system):
    with metered(system) as service:
        run = start(system)
        repo = RunRepository(system[4], system[2][0], system[3].workspace_id)
        lease = repo.claim(run["run_id"])
        repo.begin_call(lease, "unknown", usage=(service, 200, 100))
        repo.cancel(lease.run_id)
        repo.acknowledge_cancel(lease)
        service.reconcile_terminal()
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
            owner, admin = system[1]
            assert client.get("/v1/me/usage").status_code == 401
            assert client.get("/v1/me/usage", headers=owner).json()["reserved"] == 200
            assert client.get("/v1/me/usage/attempts", headers=admin).json() == []
            for path in ("/v1/admin/accounts", "/v1/admin/usage", "/v1/admin/usage/unresolved"):
                assert client.get(path, headers=owner).status_code == 403
                assert client.get(path, headers=admin).status_code == 200
            unresolved = client.get("/v1/admin/usage/unresolved", headers=admin).json()
            row = next(r for r in unresolved if r["run_id"] == run["run_id"])
            body = {
                "expected_revision": row["revision"],
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_tokens": 0,
                "reasoning_tokens": 10,
                "reason": "Synthetic provider reconciliation",
            }
            path = "/v1/admin/usage/" + row["attempt_id"] + "/reconcile"
            assert client.post(path, headers=owner, json=body).status_code == 403
            assert client.post(path, headers=admin, json=body).status_code == 200
            assert client.post(path, headers=admin, json=body).status_code == 409
            quota_path = "/v1/admin/accounts/" + system[2][0].user_id + "/quota"
            assert (
                client.put(
                    quota_path,
                    headers=admin,
                    json={
                        "monthly_tokens": 2000,
                        "expected_revision": 1,
                        "reason": "Synthetic quota change",
                    },
                ).status_code
                == 200
            )
            assert service.summary(system[4], system[2][0])["used"] == 120
            assert (
                system[-1]
                .execute(
                    "SELECT count(*) FROM agent_business.management_audit WHERE target_user_id=%s",
                    (system[2][0].user_id,),
                )
                .fetchone()[0]
                >= 2
            )


def test_active_estimate_correction_restores_reserved_budget_and_price_is_frozen(system):
    with metered(system) as service:
        run = start(system)
        repo = RunRepository(system[4], system[2][0], system[3].workspace_id)
        lease = repo.claim(run["run_id"])
        n = repo.begin_call(lease, "call", usage=(service, 200, 100))
        service.record(lease.run_id, "call", n, ChatResponse("abc"))
        repo.finish_call(lease, "call", n, ChatResponse("abc"))
        assert service.summary(system[4], system[2][0])["reserved"] == 197
        service.price = {**PRICE, "version": "new-version", "output": "100"}
        with service.control.transaction() as connection:
            service.lock(connection, system[2][0].user_id)
            row = connection.execute(
                "SELECT * FROM agent_business.usage_attempts WHERE run_id=%s", (lease.run_id,)
            ).fetchone()
            service.apply(connection, row, Usage("provider", 60, 20, 80, 0), status="returned")
        summary = service.summary(system[4], system[2][0])
        assert summary["used"] == 80 and summary["reserved"] == 220
        assert service.details(system[4], system[2][0])[0]["price_version"] == "fixture-v1"
        service.provider_key = "wrong-provider"
        with pytest.raises(RunConflict):
            repo.begin_call(lease, "new", usage=(service, 100, 50))


def test_month_rollover_preserves_previous_usage_and_rechecks_new_quota(system, monkeypatch):
    with metered(system) as service:
        monkeypatch.setattr("multiuser.usage.billing_period", lambda: date(2026, 9, 1))
        run = start(system)
        repo = RunRepository(system[4], system[2][0], system[3].workspace_id)
        lease = repo.claim(run["run_id"])
        n = repo.begin_call(lease, "sep", usage=(service, 200, 100))
        response = ChatResponse(
            "ok", metadata={"usage": {"prompt_tokens": 60, "completion_tokens": 20}}
        )
        service.record(lease.run_id, "sep", n, response)
        repo.finish_call(lease, "sep", n, response)
        monkeypatch.setattr("multiuser.usage.billing_period", lambda: date(2026, 10, 1))
        repo.begin_call(lease, "oct", usage=(service, 200, 100))
        old = (
            system[-1]
            .execute(
                "SELECT used_tokens,held_tokens FROM agent_business.quota_accounts "
                "WHERE user_id=%s AND period='2026-09-01'",
                (system[2][0].user_id,),
            )
            .fetchone()
        )
        new = (
            system[-1]
            .execute(
                "SELECT used_tokens,held_tokens FROM agent_business.quota_accounts "
                "WHERE user_id=%s AND period='2026-10-01'",
                (system[2][0].user_id,),
            )
            .fetchone()
        )
        assert old == (80, 0) and new == (0, 220)


def test_whole_executor_records_each_real_attempt_once_and_releases_unused_budget(system):
    with metered(system, budget=100000, limit=200000) as service:
        run = start(system)
        with executor(system) as worker:
            result = worker.execute(run["run_id"])
            assert result.get("committed"), result
            assert not worker.execute(run["run_id"])["accepted"]
        summary = service.summary(system[4], system[2][0])
        assert summary["reserved"] == 0 and summary["used"] > 0
        assert summary["used"] == sum(
            r["total_tokens"] for r in service.details(system[4], system[2][0])
        )
        assert summary["month_requests"] == 1


def test_manual_reconciliation_rejects_inflight_and_preserves_provider_result(system):
    with metered(system) as service:
        run = start(system)
        repo = RunRepository(system[4], system[2][0], system[3].workspace_id)
        lease = repo.claim(run["run_id"])
        n = repo.begin_call(lease, "live", usage=(service, 200, 100))
        row = (
            system[-1]
            .execute(
                "SELECT attempt_id,revision FROM agent_business.usage_attempts WHERE run_id=%s",
                (run["run_id"],),
            )
            .fetchone()
        )
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
            body = {
                "expected_revision": row[1],
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "reason": "Synthetic reconciliation must wait",
            }
            assert (
                client.post(
                    "/v1/admin/usage/" + row[0] + "/reconcile", headers=system[1][1], json=body
                ).status_code
                == 409
            )
        service.record(
            lease.run_id, "live", n, ChatResponse("ok", metadata={"usage": {"total_tokens": 120}})
        )
        assert service.summary(system[4], system[2][0])["used"] == 120


def test_authorization_management_is_versioned_audited_and_scope_enforced(system):
    with metered(system):
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
            owner, admin = system[1]
            ws = "workspace:" + uuid.uuid4().hex
            path = f"/v1/admin/workspaces/{ws}/scope"
            body = {
                "name": "Management fixture",
                "collections": ["fixture"],
                "modules": ["inbound"],
                "sites": ["DC01"],
                "environments": ["test"],
                "expected_revision": 0,
                "reason": "Synthetic approved scope",
            }
            for endpoint in ("/v1/admin/authorization", "/v1/admin/audit"):
                assert client.get(endpoint, headers=owner).status_code == 403
                assert client.get(endpoint, headers=admin).status_code == 200
            assert client.put(path, headers=owner, json=body).status_code == 403
            assert client.put(path, headers=admin, json=body).json()["revision"] == 1
            assert client.put(path, headers=admin, json=body).status_code == 409
            for change in (
                {"collections": ["*"]},
                {"sites": []},
                {"modules": ["inbound", "inbound"]},
                {"name": " "},
            ):
                assert client.put(path, headers=admin, json={**body, **change}).status_code == 422
            assert (
                client.put(
                    "/v1/admin/workspaces/workspace:legacy/scope", headers=admin, json=body
                ).status_code
                == 422
            )
            grant = {
                "user_id": system[2][0].user_id,
                "workspace_id": ws,
                "role": "workspace_admin",
                "enabled": True,
                "expected_revision": 0,
                "reason": "Synthetic member approval",
            }
            assert client.put("/v1/admin/memberships", headers=owner, json=grant).status_code == 403
            assert (
                client.put("/v1/admin/memberships", headers=admin, json=grant).json()["revision"]
                == 1
            )
            assert client.put("/v1/admin/memberships", headers=admin, json=grant).status_code == 409
            assert client.get("/v1/admin/accounts", headers=owner).status_code == 403
            own = client.get("/v1/me", headers=owner).json()
            assert ws in {w["workspace_id"] for w in own["workspaces"]}
            PostgresSessionRepository(system[4], system[2][0], ws).workspace.check(
                "sites", ["DC01"]
            )
            accepted = client.post(
                "/v1/conversations",
                headers=owner,
                json={
                    "workspace_id": ws,
                    "goal": "Synthetic protected history",
                    "answer_strategy": "standard",
                    "idempotency_key": uuid.uuid4().hex,
                },
            ).json()
            cid = accepted["conversation_id"]
            repository = PostgresSessionRepository(system[4], system[2][0], ws)
            repository.append_turn(
                session_id=cid,
                expected_revision=1,
                role="assistant",
                message="Synthetic DC01 answer",
                metadata={
                    "citations": [
                        {
                            "collection": "fixture",
                            "module": "inbound",
                            "site": "DC01",
                            "environment": "test",
                        }
                    ]
                },
            )
            assert (
                client.put(
                    path, headers=admin, json={**body, "expected_revision": 1, "sites": ["DC02"]}
                ).json()["revision"]
                == 2
            )
            with pytest.raises(ValueError):
                PostgresSessionRepository(system[4], system[2][0], ws).workspace.check(
                    "sites", ["DC01"]
                )
            assert (
                client.get(f"/v1/conversations/{cid}/workbench", headers=owner).status_code == 403
            )
            for read in (
                lambda: PostgresSessionRepository(system[4], system[2][0], ws).get_revision(cid, 1),
                lambda: PostgresSessionRepository(system[4], system[2][0], ws).list_exports(cid),
            ):
                with pytest.raises(AccessDenied):
                    read()
            assert (
                system[-1]
                .execute(
                    "SELECT count(*) FROM agent_business.turns "
                    "WHERE session_id=%s AND role='assistant'",
                    (cid,),
                )
                .fetchone()[0]
                == 1
            )
            assert (
                client.put(
                    "/v1/admin/memberships",
                    headers=admin,
                    json={**grant, "expected_revision": 1, "enabled": False},
                ).json()["revision"]
                == 2
            )
            with pytest.raises(AccessDenied):
                PostgresSessionRepository(system[4], system[2][0], ws)
            snapshot = client.get("/v1/admin/authorization", headers=admin).json()
            member = next(m for m in snapshot["memberships"] if m["workspace_id"] == ws)
            assert member["active"] is False and member["revision"] == 2
            audit = client.get("/v1/admin/audit", headers=admin).json()
            assert sum(a["resource_id"] == ws for a in audit) == 4
            assert all("chat" not in a and "state_json" not in a for a in audit)


def test_unknown_management_records_return_safe_errors(system):
    with metered(system):
        issuer = os.environ["P0_OIDC_ISSUER"]
        with TestClient(
            create_app(
                OIDCVerifier(issuer, "wms-api", allow_local_http=True),
                TokenIntrospector(issuer, "wms-api", os.environ["P1_API_CLIENT_SECRET"]),
                system[5],
            )
        ) as client:
            missing_id = uuid.uuid4().hex
            assert (
                client.put(
                    "/v1/admin/accounts/" + missing_id + "/quota",
                    headers=system[1][1],
                    json={
                        "monthly_tokens": 1000,
                        "expected_revision": 0,
                        "reason": "Synthetic absent quota target",
                    },
                ).status_code
                == 404
            )
            assert (
                client.put(
                    "/v1/admin/memberships",
                    headers=system[1][1],
                    json={
                        "user_id": missing_id,
                        "workspace_id": system[3].workspace_id,
                        "role": "member",
                        "enabled": True,
                        "expected_revision": 0,
                        "reason": "Synthetic absent membership target",
                    },
                ).status_code
                == 404
            )
            assert (
                client.put(
                    "/v1/admin/accounts/" + uuid.uuid4().hex + "/status",
                    headers=system[1][1],
                    json={"enabled": False, "reason": "Synthetic missing account"},
                ).status_code
                == 404
            )
            assert (
                client.put(
                    "/v1/admin/accounts/" + system[2][1].user_id + "/status",
                    headers=system[1][1],
                    json={"enabled": False, "reason": "Synthetic self disable rejected"},
                ).status_code
                == 403
            )
