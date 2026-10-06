"""Resource reuse factories and bounded provider transport; no credentials/model calls."""

from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest

from api import bootstrap, execution_bootstrap
from core.settings import load_settings
from libs.llm.openai_compatible_llm import LLMProviderError
from multiuser.pooled_llm import PooledLLM


def configuration(_path=None):
    settings = load_settings()
    return replace(
        settings,
        llm=replace(
            settings.llm,
            provider="openai_compatible",
            base_url="https://provider.invalid/v1",
            model="synthetic",
            timeout_seconds=5,
            api_key_env=None,
            max_retries=0,
        ),
    )


@pytest.mark.parametrize(
    "status,payload",
    [
        (200, {"choices": [{"message": {"content": "ok"}}]}),
        (429, {}),
        (503, {}),
        (200, []),
        (200, "not JSON"),
    ],
)
def test_bounded_pooled_provider_normalizes_or_marks_unknown(status, payload):
    def respond(request):
        assert request.method == "POST" and request.url.host == "provider.invalid"
        return (
            httpx.Response(status, content=payload.encode())
            if isinstance(payload, str)
            else httpx.Response(status, json=payload)
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = PooledLLM(configuration().llm, client=client)
    if status == 200 and isinstance(payload, dict):
        assert model.chat([{"role": "user", "content": "synthetic"}]).content == "ok"
    else:
        with pytest.raises(LLMProviderError):
            model.chat([{"role": "user", "content": "synthetic"}])
    model.close()


def test_provider_body_limit_and_transport_timeout():
    for transport in (
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))),
        httpx.MockTransport(lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic"))),
    ):
        model = PooledLLM(configuration().llm, client=httpx.Client(transport=transport))
        with pytest.raises(LLMProviderError):
            model.chat([{"role": "user", "content": "q"}])
        model.close()


def environment(monkeypatch):
    values = {
        "WMS_USER_DB_DSN": "synthetic",
        "WMS_CONTROL_DB_DSN": "synthetic-control",
        "WMS_OIDC_ISSUER": "https://identity.invalid/realms/wms",
        "WMS_API_CLIENT_SECRET": "synthetic",
        "WMS_SESSION_CLIENT_ID": "reader",
        "WMS_SESSION_CLIENT_SECRET": "synthetic",
        "WMS_EXECUTION_URL": "https://execute.invalid",
        "WMS_EXECUTION_TOKEN": "x" * 32,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_shared_factory_initializes_indexes_once_and_governs_retrieval(monkeypatch):
    environment(monkeypatch)
    monkeypatch.setattr(execution_bootstrap, "load_settings", configuration)
    mocks = {}
    for name in (
        "PooledLLM",
        "AccessStore",
        "RunControl",
        "SessionAuthority",
        "ModelGovernor",
        "RunExecutor",
        "create_execution_app",
        "redis_client",
    ):
        mocks[name] = Mock()
        monkeypatch.setattr(execution_bootstrap, name, mocks[name])
    mocks["RunExecutor"].return_value = "executor"
    vector = Mock()
    vector.count.return_value = 1
    sparse = Mock()
    sparse.count.return_value = 1
    monkeypatch.setattr(execution_bootstrap.VectorStoreFactory, "create", Mock(return_value=vector))
    monkeypatch.setattr(execution_bootstrap, "BM25Indexer", Mock(return_value=sparse))
    monkeypatch.setattr(execution_bootstrap.EmbeddingFactory, "create", Mock())
    monkeypatch.setattr(execution_bootstrap.RerankerFactory, "create", Mock())
    execution_bootstrap.from_environment()
    execution_bootstrap.VectorStoreFactory.create.assert_called_once()
    mocks["PooledLLM"].assert_called_once()
    assert mocks["PooledLLM"].call_args.args[0].max_retries == 0
    assert mocks["ModelGovernor"].call_count == 2
    assert (
        mocks["RunExecutor"].call_args.kwargs["retrieval_governor"]
        is mocks["ModelGovernor"].return_value
    )


def test_public_durable_api_does_not_load_models_or_indexes(monkeypatch):
    environment(monkeypatch)
    monkeypatch.setenv("WMS_P3_ENABLED", "1")
    monkeypatch.setattr(bootstrap, "load_settings", configuration)
    monkeypatch.setattr(bootstrap, "OIDCVerifier", Mock())
    monkeypatch.setattr(bootstrap, "TokenIntrospector", Mock())
    monkeypatch.setattr(bootstrap, "AccessStore", Mock())
    monkeypatch.setattr(bootstrap, "ImageStorage", Mock())
    monkeypatch.setattr(bootstrap, "AccountClient", Mock())
    monkeypatch.setattr(
        bootstrap.VectorStoreFactory,
        "create",
        Mock(side_effect=AssertionError("Public API must not load indexes")),
    )
    execution_client = Mock()
    execution_client.request.return_value = {"ready": True, "protocol": "wms-runs-v1"}
    monkeypatch.setattr("workers.runs.ExecutionClient", Mock(return_value=execution_client))
    app = bootstrap.from_environment()
    assert any(route.path == "/v1/runs/{run_id}/events" for route in app.routes)
    execution_client.request.assert_called_once_with("/internal/ready")
    bootstrap.VectorStoreFactory.create.assert_not_called()


def test_unsupported_provider_and_invalid_limits_fail_before_public_execution(monkeypatch):
    monkeypatch.setattr(
        execution_bootstrap,
        "load_settings",
        lambda _path=None: replace(
            configuration(), llm=replace(configuration().llm, provider="disabled")
        ),
    )
    with pytest.raises(ValueError):
        execution_bootstrap.from_environment()
    monkeypatch.setenv("WMS_MODEL_INFLIGHT", "0")
    with pytest.raises(ValueError):
        execution_bootstrap.execution_settings()
