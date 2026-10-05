import json
from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from agents.contracts import Evidence
from agents.tools import KnowledgeSearchResult
from agents.workspace import Workspace, WorkspaceScopeError
from api.users import create_app
from multiuser.access import AccessDenied, UserContext, context_values
from multiuser.agent import GuardedKnowledge, GuardedLLM
from multiuser.identity import IdentityUnavailable, InvalidIdentity, Principal
from multiuser.session_auth import TokenIntrospector
from multiuser.session_repository import Connection, Row
from multiuser.tools import parse, schemas


def test_introspection_uses_fixed_origin_and_checks_active_subject():
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"active": True, "sub": "A"})

    intro = TokenIntrospector(
        "https://id.example.invalid/realm",
        "api",
        "synthetic",
        client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    intro.check("synthetic-token", Principal("https://id.example.invalid/realm", "A"))
    assert calls[0].url.path == "/realm/protocol/openid-connect/token/introspect"
    assert calls[0].method == "POST"
    assert "synthetic-token" not in str(calls[0].url)
    intro.close()


@pytest.mark.parametrize(
    "value", [{"active": False}, {"active": True, "sub": "B"}, {"active": "true", "sub": "A"}]
)
def test_introspection_denies_inactive_or_other_subject(value):
    with TokenIntrospector(
        "https://id.example.invalid",
        "api",
        "synthetic",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value))
        ),
    ).client as client:
        intro = TokenIntrospector("https://id.example.invalid", "api", "synthetic", client=client)
        with pytest.raises(InvalidIdentity):
            intro.check("synthetic", Principal("https://id.example.invalid", "A"))


@pytest.mark.parametrize("status", [302, 500])
def test_introspection_service_failure_is_closed(status):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, json={})))
    intro = TokenIntrospector("https://id.example.invalid", "api", "synthetic", client=client)
    with pytest.raises(IdentityUnavailable):
        intro.check("synthetic", Principal("https://id.example.invalid", "A"))
    intro.close()
    with pytest.raises(ValueError):
        TokenIntrospector("https://id.example.invalid", "api", "")


@pytest.mark.parametrize("body", [b"null", b"[]", b"not-json"])
def test_malformed_success_response_is_service_unavailable_not_internal_error(body):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )
    intro = TokenIntrospector("https://id.example.invalid", "api", "synthetic", client=client)
    with pytest.raises(IdentityUnavailable):
        intro.check("synthetic", Principal("https://id.example.invalid", "A"))
    intro.close()


@pytest.mark.parametrize(
    "error",
    [
        AccessDenied("PRIVATE permission"),
        RuntimeError("PRIVATE details"),
        ValueError("PRIVATE details"),
    ],
)
def test_mcp_execution_errors_are_opaque_tool_results_and_unknown_tools_protocol_errors(error):
    verifier, intro, application = Mock(), Mock(), Mock()
    application.store.resolve.return_value = UserContext("A", "issuer", "sub", "sid", 1, 2)
    application.tool.side_effect = error
    headers = {"Authorization": "Bearer synthetic"}
    with TestClient(create_app(verifier, intro, application)) as client:
        result = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_configuration_session",
                    "arguments": {"session_id": "private"},
                },
            },
        )
        assert result.status_code == 200
        assert result.json()["id"] == 1 and result.json()["result"]["isError"] is True
        assert "PRIVATE" not in result.text
        result = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "host_process", "arguments": {}},
            },
        )
        assert result.status_code == 200
        assert result.json()["error"]["code"] == -32602


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("host_process", {}),
        (
            "start_configuration_session",
            {"goal": "test", "workspace_id": "workspace:a", "user_id": "A"},
        ),
        (
            "continue_configuration_session",
            {"session_id": "s", "expected_revision": True, "message": "hi"},
        ),
        ("get_configuration_session", {"session_id": "s", "thread_id": "other"}),
    ],
)
def test_tools_deny_arbitrary_registry_identity_and_thread_fields(name, arguments):
    with pytest.raises(ValueError):
        parse(name, arguments)
    assert all(t["inputSchema"]["additionalProperties"] is False for t in schemas())


def test_guarded_model_stops_after_revocation_even_if_model_finished():
    guard = Mock(side_effect=[None, AccessDenied("revoked")])
    model = Mock()
    with pytest.raises(AccessDenied):
        GuardedLLM(model, guard).chat([])
    assert model.chat.call_count == 1
    guard = Mock(side_effect=AccessDenied("disabled"))
    with pytest.raises(AccessDenied):
        GuardedLLM(model, guard).chat([])
    assert model.chat.call_count == 1


def test_retrieval_scope_applied_before_call_and_results_filtered_before_model():
    workspace = Workspace("workspace:a", "A", ("allowed",), ("inbound",), ("DC01",), ("test",))
    good = Evidence(
        "e:1",
        "c:1",
        "fixture.pdf",
        "synthetic",
        1,
        module="inbound",
        site="DC01",
        environment="test",
        collection="allowed",
    )
    bad = replace(good, evidence_id="e:2", collection="private-other")
    adapter = Mock()
    adapter.search.return_value = KnowledgeSearchResult("question", {}, (good, bad), True, ())
    guarded = GuardedKnowledge(adapter, Mock(), workspace)
    result = guarded.search("question", filters={"module": "inbound"})
    assert result.evidence == (good,)
    assert adapter.search.call_args.kwargs["filters"]["collection"] == "allowed"
    with pytest.raises(WorkspaceScopeError):
        guarded.search("question", filters={"collection": "other"})
    assert adapter.search.call_count == 1


def test_transaction_identity_contains_expiry_but_not_caller_role():
    context = UserContext("A", "issuer", "subject", "sid", 1, 2)
    assert context_values(context)["app.token_exp"] == "2"
    assert not any("role" in k for k in context_values(context))
    assert Row({"a": 1, "b": 2})[0] == 1
    assert Row({"a": 1})["a"] == 1
    connection = Mock()
    bridge = Connection(connection)
    bridge.execute("INSERT OR IGNORE INTO fixture VALUES (?)", (1,))
    assert connection.execute.call_args.args[0].endswith("ON CONFLICT DO NOTHING")
    bridge.execute("SELECT * FROM fixture LIMIT ?", (-1,))
    assert connection.execute.call_args.args[1] == (None,)


@pytest.mark.parametrize(
    "error,status", [(InvalidIdentity("bad"), 401), (IdentityUnavailable("down"), 503)]
)
def test_api_auth_errors_are_opaque_and_body_identity_never_authenticates(error, status):
    verifier, intro, application = Mock(), Mock(), Mock()
    verifier.verify.side_effect = error
    with TestClient(create_app(verifier, intro, application)) as client:
        assert client.get("/v1/me?user_id=A").status_code == 401
        result = client.get("/v1/me", headers={"Authorization": "Bearer synthetic"})
        assert result.status_code == status
        assert "bad" not in result.text and "down" not in result.text
        application.store.resolve.assert_not_called()


def test_api_origin_protocol_and_payload_validation():
    verifier, intro, application = Mock(), Mock(), Mock()
    application.store.resolve.return_value = UserContext("A", "issuer", "sub", "sid", 1, 2)
    headers = {"Authorization": "Bearer synthetic"}
    with TestClient(
        create_app(verifier, intro, application, allowed_origins=("https://wms.example.invalid",))
    ) as client:
        assert (
            client.post(
                "/mcp",
                headers={**headers, "MCP-Protocol-Version": "fake"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "unsupported"}
            ).json()["error"]["code"]
            == -32601
        )
        assert (
            client.post(
                "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "ping"}
            ).status_code
            == 400
        )
        result = client.post(
            "/mcp",
            headers={**headers, "Origin": "https://wms.example.invalid"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert result.json()["result"] == {}
        assert result.headers["cache-control"] == "no-store"
        assert (
            client.post(
                "/v1/conversations",
                headers=headers,
                json={"goal": "hi", "workspace_id": "workspace:a", "owner_user_id": "B"},
            ).status_code
            == 422
        )
    assert json.dumps(schemas())


@pytest.mark.parametrize("empty_index", [False, True])
def test_bootstrap_composes_scoped_service_or_closes_on_start_failure(monkeypatch, empty_index):
    from api import bootstrap
    from core.settings import load_settings

    monkeypatch.setenv("WMS_OIDC_ISSUER", "https://id.example.invalid")
    monkeypatch.setenv("WMS_USER_DB_DSN", "synthetic-runtime-dsn")
    monkeypatch.setenv("WMS_API_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setenv("WMS_WEB_ORIGINS", "https://wms.example.invalid")
    verifier, intro, store, vector, sparse, agent = [Mock() for _ in range(6)]
    monkeypatch.setattr(bootstrap, "OIDCVerifier", Mock(return_value=verifier))
    monkeypatch.setattr(bootstrap, "TokenIntrospector", Mock(return_value=intro))
    monkeypatch.setattr(bootstrap, "AccessStore", Mock(return_value=store))
    monkeypatch.setattr(bootstrap, "load_settings", Mock(return_value=load_settings()))
    monkeypatch.setattr(bootstrap.VectorStoreFactory, "create", Mock(return_value=vector))
    monkeypatch.setattr(bootstrap, "BM25Indexer", Mock(return_value=sparse))
    vector.count.return_value = 0 if empty_index else 1
    sparse.count.return_value = 1
    for factory in (bootstrap.EmbeddingFactory, bootstrap.RerankerFactory, bootstrap.LLMFactory):
        monkeypatch.setattr(factory, "create", Mock(return_value=Mock()))
    monkeypatch.setattr(bootstrap, "HybridSearch", Mock(return_value=Mock()))
    monkeypatch.setattr(bootstrap, "UserAgent", Mock(return_value=agent))
    if empty_index:
        with pytest.raises(RuntimeError, match="index is required"):
            bootstrap.from_environment()
        store.close.assert_called_once()
        intro.close.assert_called_once()
        verifier.close.assert_called_once()
    else:
        with TestClient(bootstrap.from_environment()) as client:
            result = client.get("/v1/me", headers={"Origin": "https://wms.example.invalid"})
            assert result.status_code == 401
        store.close.assert_called_once()
