"""Real OIDC + PostgreSQL non-owner RLS, using only disposable synthetic data."""

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, replace

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command
from test_multiuser_p0_live import login

from agents.repositories import SessionNotFoundError, SessionRevisionConflict
from agents.runtime import build_runtime_probe_graph
from agents.workspace import Workspace
from api.users import create_app
from core.settings import load_settings
from libs.llm import ChatResponse
from multiuser.access import AccessDenied, AccessStore
from multiuser.agent import UserAgent
from multiuser.application import OwnedApplication
from multiuser.identity import OIDCVerifier
from multiuser.session_auth import TokenIntrospector
from multiuser.session_repository import PostgresSessionRepository

pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P1_LIVE") != "1", reason="P1 requires disposable OIDC and PostgreSQL"
)


@pytest.fixture(scope="module")
def tokens():
    return [
        login(name, os.environ[key])
        for name, key in (("user-a", "P0_A_PASSWORD"), ("user-b", "P0_B_PASSWORD"))
    ]


@pytest.fixture
def system(tokens, tmp_path):
    issuer = os.environ["P0_OIDC_ISSUER"]
    verifier = OIDCVerifier(issuer, "wms-api", allow_local_http=True)
    intro = TokenIntrospector(issuer, "wms-api", os.environ["P1_API_CLIENT_SECRET"])
    store = AccessStore(os.environ["P1_POSTGRES_DSN"], max_connections=2)
    contexts = [store.resolve(verifier.verify(token)) for token in tokens]
    workspace = Workspace(
        "workspace:" + uuid.uuid4().hex,
        "Synthetic shared scope",
        ("fixture",),
        ("inbound",),
        ("DC01",),
        ("test",),
    )
    with psycopg.connect(os.environ["P0_POSTGRES_DSN"], autocommit=True) as admin:
        admin.execute(
            "INSERT INTO identity_business.workspaces VALUES(%s,%s)",
            (workspace.workspace_id, json.dumps(asdict(workspace))),
        )
        for index, context in enumerate(contexts):
            admin.execute(
                "INSERT INTO identity_business.memberships VALUES(%s,%s,%s,true)",
                (
                    context.user_id,
                    workspace.workspace_id,
                    "reviewer" if index == 0 else "workspace_admin",
                ),
            )
        # Platform admin status must not grant access to other people's private conversations.
        admin.execute(
            "UPDATE identity_business.users SET is_platform_admin=true WHERE user_id=%s",
            (contexts[1].user_id,),
        )
        application = OwnedApplication(store, export_root=tmp_path)
        with TestClient(create_app(verifier, intro, application)) as client:
            headers = [{"Authorization": "Bearer " + t} for t in tokens]
            yield client, headers, contexts, workspace, store, application, admin
    store.close()


def new(system, index=0):
    client, headers, _, workspace, _, _, _ = system
    result = client.post(
        "/v1/conversations",
        headers=headers[index],
        json={"goal": "Synthetic private configuration", "workspace_id": workspace.workspace_id},
    )
    assert result.status_code == 200, result.text
    return result.json()["session_id"]


def test_real_login_profile_no_auto_membership_or_untrusted_identity(system):
    client, headers, contexts, workspace, store, _, _ = system
    assert contexts[0].user_id != contexts[1].user_id
    assert client.get("/v1/me").status_code == 401
    assert (
        client.get(
            "/v1/me", headers={**headers[0], "Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert client.patch("/v1/me", headers=headers[0], json={"nickname": "Alice"}).status_code == 200
    assert client.get("/v1/me", headers=headers[0]).json()["nickname"] == "Alice"
    assert (
        client.patch(
            "/v1/me", headers=headers[0], json={"status": "active", "is_platform_admin": True}
        ).status_code
        == 422
    )
    for field in ("owner_user_id", "user_id", "roles", "thread_id", "checkpoint_ns"):
        result = client.post(
            "/v1/conversations",
            headers=headers[0],
            json={"goal": "Synthetic", "workspace_id": workspace.workspace_id, field: "fake"},
        )
        assert result.status_code == 422
    for scope in ("workspace:legacy", "workspace:ungranted"):
        assert (
            client.post(
                "/v1/conversations",
                headers=headers[0],
                json={"goal": "Synthetic", "workspace_id": scope},
            ).status_code
            == 403
        )
    with pytest.raises(ValueError, match="must not own"):
        AccessStore(os.environ["P0_POSTGRES_DSN"])
    with pytest.raises(TypeError), store.transaction({"user_id": contexts[0].user_id}):
        pass


@pytest.mark.parametrize("index", [0, 1])
def test_every_http_and_tool_parent_is_private_even_for_admin(system, index):
    client, headers, contexts, workspace, store, application, _ = system
    session_id = new(system, index)
    repository = application.repository(contexts[index], session_id)
    repository.append_turn(
        session_id=session_id, expected_revision=1, role="user", message="PRIVATE"
    )
    for kind in ("run", "event", "usage", "evidence", "image", "attachment"):
        repository.put_resource(session_id, kind + session_id, kind, {"private": "PRIVATE"})
    other = headers[1 - index]
    root = "/v1/conversations/" + session_id
    requests = [
        ("GET", root, None),
        ("GET", root + "/revisions/1", None),
        ("PATCH", root, {"title": "Hijack"}),
        ("DELETE", root, None),
        ("POST", root + "/restore", None),
        ("POST", root + "/archive", None),
        ("POST", root + "/unarchive", None),
        ("POST", root + "/continue", {"message": "Hijack", "expected_revision": 1}),
        ("POST", root + "/validate", {"expected_revision": 1}),
        (
            "POST",
            root + "/review",
            {"expected_revision": 1, "decision": "approve", "comment": "Hijack"},
        ),
        ("POST", root + "/exports", {"expected_revision": 1}),
        ("GET", root + "/exports", None),
        ("GET", root + "/exports/fake/download", None),
        ("POST", root + "/feedback", {"revision": 1, "kind": "thumbs_up"}),
        ("GET", root + "/feedback/1", None),
        ("GET", root + "/events", None),
    ]
    requests += [
        ("GET", root + f"/resources/{kind}/{kind}{session_id}", None)
        for kind in ("run", "event", "usage", "evidence", "image", "attachment")
    ]
    requests += [
        ("GET", root + f"/files/{kind}/{kind}{session_id}", None)
        for kind in ("image", "attachment")
    ]
    for method, url, body in requests:
        result = client.request(method, url, headers=other, json=body)
        assert result.status_code == 404, (method, url, result.status_code)
        assert "PRIVATE" not in result.text
    listing = client.get(
        "/v1/conversations", params={"workspace_id": workspace.workspace_id}, headers=other
    )
    assert session_id not in listing.text
    with store.transaction(contexts[1 - index]) as connection:
        for table in ("sessions", "revisions", "turns", "resources", "checkpoint_threads"):
            assert not connection.execute(
                f"SELECT * FROM {table} WHERE session_id=%s", (session_id,)
            ).fetchall()
    arguments = {"session_id": session_id}
    rest_denied = client.post(
        "/v1/tools/call",
        headers=other,
        json={"name": "get_configuration_session", "arguments": arguments},
    )
    assert rest_denied.status_code == 404
    mcp_denied = client.post(
        "/mcp",
        headers=other,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_configuration_session", "arguments": arguments},
        },
    )
    assert mcp_denied.status_code == 200
    assert mcp_denied.json()["result"]["isError"] is True
    assert "PRIVATE" not in mcp_denied.text


def test_pool_reset_no_context_and_immutable_bindings(system):
    _, _, contexts, workspace, store, application, _ = system
    session_id = new(system)
    with store.transaction(contexts[0]) as connection:
        assert connection.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] >= 1
    with store.pool.connection() as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM agent_business.sessions").fetchone()["n"]
            == 0
        )
        assert (
            connection.execute(
                "SELECT nullif(current_setting('app.user_id',true),'') AS u"
            ).fetchone()["u"]
            is None
        )
    with store.transaction(contexts[1]) as connection:
        assert not connection.execute(
            "SELECT * FROM sessions WHERE session_id=%s", (session_id,)
        ).fetchone()
    for field, value in (
        ("owner_user_id", contexts[1].user_id),
        ("workspace_id", "workspace:fake"),
        ("checkpoint_thread_id", "fake-thread"),
    ):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            store.transaction(contexts[0]) as connection,
        ):
            connection.execute(
                f"UPDATE sessions SET {field}=%s WHERE session_id=%s", (value, session_id)
            )
    repository = application.repository(contexts[0], session_id)
    with pytest.raises(SessionRevisionConflict):
        repository.update_revision(
            session_id=session_id,
            expected_revision=9,
            state_update={},
            actor="system",
            reason="fixture",
        )
    with pytest.raises(ValueError):
        repository.list_sessions(limit=101)
    with pytest.raises(ValueError):
        repository.search_sessions("x" * 201)
    assert PostgresSessionRepository(store, contexts[0], workspace.workspace_id).list_sessions()


def test_owned_search_archive_trash_restore_and_purge_are_atomic(system):
    client, headers, contexts, workspace, _, application, _ = system
    a, b = new(system), new(system, 1)
    root = "/v1/conversations/" + a
    assert client.patch(root, headers=headers[0], json={"title": "Trolley"}).status_code == 200
    params = {"workspace_id": workspace.workspace_id, "q": "Trolley"}
    assert a in client.get("/v1/conversations", headers=headers[0], params=params).text
    assert a not in client.get("/v1/conversations", headers=headers[1], params=params).text
    assert client.post(root + "/archive", headers=headers[0]).status_code == 200
    assert a not in client.get("/v1/conversations", headers=headers[0], params=params).text
    assert (
        a
        in client.get(
            "/v1/conversations", headers=headers[0], params={**params, "archived": True}
        ).text
    )
    assert client.post(root + "/unarchive", headers=headers[0]).status_code == 200
    assert client.delete(root, headers=headers[0]).status_code == 200
    assert client.get(root, headers=headers[0]).status_code == 404
    assert a in client.get("/v1/trash", headers=headers[0], params=params).text
    assert a not in client.get("/v1/trash", headers=headers[1], params=params).text
    result = client.post(
        "/v1/trash/purge",
        headers=headers[0],
        params=params,
        json={"expected_revisions": {a: 1, b: 1}},
    )
    assert result.status_code == 404
    assert client.post(root + "/restore", headers=headers[0]).status_code == 200
    assert client.delete(root, headers=headers[0]).status_code == 200
    repository = application.repository(contexts[0], a)
    with pytest.raises(ValueError):
        repository.purge_deleted_sessions({a: 1}, checkpoint_path="unsafe")
    assert client.post(
        "/v1/trash/purge", headers=headers[0], params=params, json={"expected_revisions": {a: 1}}
    ).json() == {"purged": 1}
    assert client.post(root + "/restore", headers=headers[0]).status_code == 404


def test_owned_feedback_validation_review_and_export_download(system):
    client, headers, contexts, workspace, _, application, admin = system
    session_id = new(system)
    root = "/v1/conversations/" + session_id
    repository = application.repository(contexts[0], session_id)
    result = client.post(root + "/validate", headers=headers[0], json={"expected_revision": 1})
    assert result.status_code == 200, result.text
    revision = result.json()["revision"]
    repository.update_revision(
        session_id=session_id,
        expected_revision=revision,
        state_update={
            "status": "review_required",
            "conflicts": [],
            "validation_findings": [],
            "conversation_summary": "PRIVATE MEMORY",
        },
        actor="system",
        reason="fixture",
    )
    revision += 1
    for _ in range(2):
        assert (
            client.post(
                root + "/feedback",
                headers=headers[0],
                json={"revision": revision, "kind": "thumbs_up"},
            ).status_code
            == 200
        )
    assert client.get(root + f"/feedback/{revision}", headers=headers[0]).json()["total"] == 1
    assert (
        client.post(
            root + "/feedback",
            headers=headers[0],
            json={"revision": revision, "kind": "regeneration", "reason": "bad"},
        ).status_code
        == 422
    )
    admin.execute(
        "UPDATE identity_business.memberships SET role='member' "
        "WHERE user_id=%s AND workspace_id=%s",
        (contexts[0].user_id, workspace.workspace_id),
    )
    body = {"expected_revision": revision, "decision": "approve", "comment": "Synthetic review"}
    assert client.post(root + "/review", headers=headers[0], json=body).status_code == 403
    admin.execute(
        "UPDATE identity_business.memberships SET role='reviewer' "
        "WHERE user_id=%s AND workspace_id=%s",
        (contexts[0].user_id, workspace.workspace_id),
    )
    result = client.post(root + "/review", headers=headers[0], json=body)
    assert result.status_code == 200, result.text
    revision = result.json()["revision"]
    for format in ("json", "markdown"):
        result = client.post(
            root + "/exports",
            headers=headers[0],
            json={"expected_revision": revision, "format": format},
        )
        assert result.status_code == 200, result.text
        assert "path" not in result.json()
        export_id = result.json()["export_id"]
        download = root + "/exports/" + export_id + "/download"
        assert client.get(download, headers=headers[0]).status_code == 200
        assert client.get(download, headers=headers[1]).status_code == 404
    assert len(client.get(root + "/exports", headers=headers[0]).json()) == 2


def test_owned_files_events_and_cache_headers(system):
    client, headers, contexts, _, _, application, _ = system
    session_id = new(system)
    repository = application.repository(contexts[0], session_id)
    user_root = application.export_root / contexts[0].user_id
    user_root.mkdir(parents=True)
    (user_root / "fixture.txt").write_text("Synthetic private attachment", encoding="utf-8")
    repository.put_resource(session_id, "fixture:" + session_id, "attachment", {}, "fixture.txt")
    root = "/v1/conversations/" + session_id
    result = client.get(root + "/files/attachment/fixture:" + session_id, headers=headers[0])
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["content-disposition"].startswith("attachment;")
    assert result.headers["x-content-type-options"] == "nosniff"
    repository.put_resource(session_id, "escape:" + session_id, "image", {}, "../fixture.txt")
    assert (
        client.get(root + "/files/image/escape:" + session_id, headers=headers[0]).status_code
        == 403
    )
    repository.put_resource(session_id, "event:" + session_id, "event", {"phase": "synthetic"})
    assert '"phase":"synthetic"' in client.get(root + "/events", headers=headers[0]).text
    assert client.get(root + "/resources/event/event:" + session_id, headers=headers[0]).json() == {
        "phase": "synthetic"
    }
    assert client.get(root + "/resources/unknown/fake", headers=headers[0]).status_code == 404
    assert client.get(root + "/files/unknown/fake", headers=headers[0]).status_code == 404


def test_actual_postgres_checkpoint_owner_resume_reopen_and_deleted_guard(system):
    client, headers, contexts, _, store, application, _ = system
    session_id = new(system)
    config = {"configurable": {"thread_id": session_id}}
    agent = UserAgent(store, os.environ["P1_POSTGRES_DSN"], None, load_settings().agent)

    async def run():
        async with agent.saver(contexts[0]) as saver:
            with pytest.raises(PermissionError):
                await saver.setup()
            graph = build_runtime_probe_graph(saver)
            await graph.ainvoke({"subject": "PRIVATE checkpoint"}, config)
            assert (await graph.aget_state(config)).next == ("approval",)
        async with agent.saver(contexts[1]) as saver:
            assert await saver.aget_tuple(config) is None
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await build_runtime_probe_graph(saver).ainvoke({"subject": "hijack"}, config)
        async with agent.saver(contexts[0]) as saver:
            graph = build_runtime_probe_graph(saver)
            await graph.ainvoke(Command(resume={"approved": True}), config)
            assert (await graph.aget_state(config)).values["result"] == "approved"
        assert (
            client.delete("/v1/conversations/" + session_id, headers=headers[0]).status_code == 200
        )
        async with agent.saver(contexts[0]) as saver:
            assert await saver.aget_tuple(config) is None
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await build_runtime_probe_graph(saver).ainvoke({"subject": "late"}, config)

    asyncio.run(run())
    application.repository(contexts[0], session_id).purge_deleted_sessions({session_id: 1})


@pytest.mark.parametrize(
    "change", ["disabled", "membership", "password_reset", "expired", "policy"]
)
def test_cached_context_stops_after_revocation_or_permission_change(system, change):
    _, _, contexts, workspace, store, application, admin = system
    session_id = new(system)
    repository = application.repository(contexts[0], session_id)
    original = contexts[0]
    if change == "disabled":
        admin.execute(
            "UPDATE identity_business.users SET status='disabled' WHERE user_id=%s",
            (original.user_id,),
        )
    elif change == "membership":
        admin.execute(
            "UPDATE identity_business.memberships SET active=false "
            "WHERE user_id=%s AND workspace_id=%s",
            (original.user_id, workspace.workspace_id),
        )
    elif change == "password_reset":
        admin.execute(
            "UPDATE identity_business.users SET revoked_before=%s WHERE user_id=%s",
            (int(time.time()) + 1, original.user_id),
        )
    elif change == "expired":
        repository.context = replace(original, expires_at=1)
    else:
        altered = {**asdict(workspace), "collections": ["revoked"]}
        admin.execute(
            "UPDATE identity_business.workspaces SET policy_json=%s WHERE workspace_id=%s",
            (json.dumps(altered), workspace.workspace_id),
        )
    try:
        with pytest.raises((AccessDenied, SessionNotFoundError)):
            repository.get_session(session_id)
        with pytest.raises((AccessDenied, SessionNotFoundError)):
            repository.append_turn(
                session_id=session_id, expected_revision=1, role="user", message="late"
            )
        if change in {"disabled", "password_reset", "expired"}:

            async def checkpoint():
                agent = UserAgent(store, os.environ["P1_POSTGRES_DSN"], None, load_settings().agent)
                async with agent.saver(repository.context) as saver:
                    await saver.aget_tuple({"configurable": {"thread_id": session_id}})

            with pytest.raises(AccessDenied):
                asyncio.run(checkpoint())
    finally:
        admin.execute(
            "UPDATE identity_business.users SET status='active',revoked_before=0 WHERE user_id=%s",
            (original.user_id,),
        )
        admin.execute(
            "UPDATE identity_business.memberships SET active=true "
            "WHERE user_id=%s AND workspace_id=%s",
            (original.user_id, workspace.workspace_id),
        )


def test_authenticated_mcp_has_no_host_fallback_and_logout_invalidates_old_token(system):
    client, headers, contexts, _, _, _, admin = system
    session_id = new(system)
    assert (
        client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    )
    assert client.get("/mcp", headers=headers[0]).status_code == 405
    assert (
        client.post(
            "/mcp",
            headers=headers[0],
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
        ).json()["result"]["protocolVersion"]
        == "2025-11-25"
    )
    assert (
        client.post(
            "/mcp",
            headers=headers[0],
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        ).status_code
        == 202
    )
    assert client.post(
        "/mcp", headers=headers[0], json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    ).json()["result"]["tools"]
    result = client.post(
        "/mcp",
        headers=headers[0],
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_configuration_session",
                "arguments": {"session_id": session_id},
            },
        },
    )
    assert result.status_code == 200
    assert result.json()["result"]["structuredContent"]["session"]["session_id"] == session_id
    for name, arguments in (
        ("query_knowledge_hub", {}),
        (
            "get_configuration_session",
            {"session_id": session_id, "owner_user_id": contexts[1].user_id},
        ),
    ):
        assert (
            client.post(
                "/v1/tools/call", headers=headers[0], json={"name": name, "arguments": arguments}
            ).status_code
            == 422
        )
    try:
        assert client.post("/v1/logout", headers=headers[0]).status_code == 200
        assert client.get("/v1/me", headers=headers[0]).status_code == 403
        assert client.get("/v1/me", headers=headers[1]).status_code == 200
    finally:
        admin.execute(
            "DELETE FROM identity_business.revoked_sessions WHERE user_id=%s AND sid=%s",
            (contexts[0].user_id, contexts[0].sid),
        )


def test_existing_agent_two_turns_use_private_pg_memory_and_checkpoints(system):
    client, headers, contexts, workspace, store, application, _ = system

    class Scripted:
        def __init__(self):
            self.outputs = [
                {
                    "confirmed_context": {"business_process": "Inbound", "modules": ["inbound"]},
                    "assumptions": [],
                    "summary": "PRIVATE PLAN",
                },
                {
                    "confirmed_context": {"product_version": "2024.1"},
                    "assumptions": [],
                    "summary": "PRIVATE VERSION",
                },
            ]

        def chat(self, messages, trace=None):
            return ChatResponse(json.dumps(self.outputs.pop(0)), model="synthetic")

    application.agent = UserAgent(
        store, os.environ["P1_POSTGRES_DSN"], Scripted(), load_settings().agent
    )
    result = client.post(
        "/v1/conversations",
        headers=headers[0],
        json={
            "goal": "Build a complete inbound configuration plan",
            "workspace_id": workspace.workspace_id,
        },
    )
    assert result.status_code == 200, result.text
    session_id = result.json()["session"]["session_id"]
    revision = result.json()["revision"]
    result = client.post(
        "/v1/conversations/" + session_id + "/continue",
        headers=headers[0],
        json={"message": "Version 2024.1", "expected_revision": revision},
    )
    assert result.status_code == 200, result.text
    assert result.json()["revision"] > revision
    repository = application.repository(contexts[0], session_id)
    assert len(repository.list_turns(session_id)) >= 4
    assert (
        repository.get_revision(session_id).state["confirmed_context"]["product_version"]
        == "2024.1"
    )
    assert client.get("/v1/conversations/" + session_id, headers=headers[1]).status_code == 404


def test_identity_provider_logout_revokes_still_signed_access_token(system):
    client, _, _, _, _, _, _ = system
    issuer = os.environ["P0_OIDC_ISSUER"]
    credentials = login("user-a", os.environ["P0_A_PASSWORD"], include_tokens=True)
    headers = {"Authorization": "Bearer " + credentials["access_token"]}
    assert client.get("/v1/me", headers=headers).status_code == 200
    result = httpx.post(
        issuer + "/protocol/openid-connect/logout",
        data={"client_id": "wms-p0-cli", "refresh_token": credentials["refresh_token"]},
        timeout=10,
    )
    assert result.status_code == 204
    assert client.get("/v1/me", headers=headers).status_code == 401
