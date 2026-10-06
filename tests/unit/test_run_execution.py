"""No-cost transport, authority, staging and model-gateway contracts."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from agents.repositories import SessionRepository
from libs.llm import ChatResponse
from libs.llm.openai_compatible_llm import LLMProviderError
from multiuser.access import AccessDenied, UserContext
from multiuser.executor import SharedKnowledge
from multiuser.governor import GovernedLLM, ModelLimits
from multiuser.identity import IdentityUnavailable
from multiuser.session_authority import SessionAuthority
from multiuser.staged_session import StagedSessionRepository
from workers.runs import ExecutionClient, TrustedWorker

CONTEXT = UserContext("user-a", "https://identity.invalid/realms/wms", "a/b", "SID", 100, 1000)


@pytest.mark.parametrize(
    "result", ["active", "ended", "disabled", "watermark", "malformed", "outage"]
)
def test_sid_authority_uses_fixed_origin_and_read_only_service_account(result):
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "synthetic-service-token", "expires_in": 60}
            )
        if result == "outage":
            return httpx.Response(503)
        if request.url.path.endswith("/sessions"):
            return httpx.Response(200, json=([{"id": "SID"}] if result != "ended" else []))
        return httpx.Response(
            200,
            json=[]
            if result == "malformed"
            else {
                "enabled": result != "disabled",
                "notBefore": 100 if result == "watermark" else 0,
            },
        )

    authority = SessionAuthority(
        CONTEXT.issuer,
        "reader",
        "synthetic-secret",
        client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    if result == "active":
        authority.check(CONTEXT)
        authority.check(CONTEXT)
        assert sum(c.url.path.endswith("/token") for c in calls) == 1
    else:
        with pytest.raises(
            IdentityUnavailable if result in {"malformed", "outage"} else AccessDenied
        ):
            authority.check(CONTEXT)
    assert all(c.method == "GET" for c in calls if not c.url.path.endswith("/token"))
    assert all(c.url.host == "identity.invalid" for c in calls)
    assert any("a%2Fb" in str(c.url) for c in calls)
    with pytest.raises(AccessDenied):
        authority.check(replace(CONTEXT, issuer="https://attacker.invalid"))
    authority.close()


@pytest.mark.parametrize(
    "issuer",
    [
        "http://remote.invalid/realms/a",
        "https://id.invalid/not-a-realm",
        "https://u:p@id.invalid/realms/a",
        "https://id.invalid/realms/a?x=1",
    ],
)
def test_authority_rejects_untrusted_issuer(issuer):
    with pytest.raises(ValueError):
        SessionAuthority(issuer, "reader", "synthetic")


@pytest.mark.parametrize("body", [{}, {"access_token": "x", "expires_in": True}, []])
def test_malformed_session_service_token_fails_closed(body):
    authority = SessionAuthority(
        CONTEXT.issuer,
        "reader",
        "synthetic",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
        ),
    )
    with pytest.raises(IdentityUnavailable):
        authority.check(CONTEXT)
    authority.close()


def test_staging_does_not_persist_partial_turns(tmp_path):
    repository = SessionRepository(tmp_path / "stage.db")
    repository.create_session(session_id="s", goal="question")
    staged = StagedSessionRepository(repository, "s", "r")
    assert staged.create_session(goal="followup", session_id="s").goal == "followup"
    staged.user_turn("question")
    staged.user_turn("question")
    assert len(staged.pending) == 1
    assert repository.list_turns("s") == ()
    revision = staged.update_revision(
        session_id="s",
        expected_revision=1,
        state_update={"status": "paused"},
        actor="agent",
        reason="verified",
    )
    assert staged.get_revision("s", 2) == revision
    assert staged.get_revision("s", 1).revision == 1
    assert repository.get_session("s").current_revision == 1
    with pytest.raises(ValueError):
        staged.update_revision(
            session_id="s", expected_revision=1, state_update={}, actor="agent", reason="duplicate"
        )
    with pytest.raises(ValueError):
        staged.get_session("other")
    with pytest.raises(ValueError):
        staged.append_turn(session_id="s", expected_revision=2, role="user", message="changed")


def gateway(delegate=None):
    repository = Mock()
    repository.execution.return_value = {"open_model_calls": 0}
    repository.cached_call.return_value = None
    repository.begin_call.return_value = 1
    limits = ModelLimits()
    governor = Mock(limits=limits)
    governor.acquire.return_value = "permit"
    delegate = delegate or Mock(max_retries=0, max_tokens=100)
    delegate.chat.return_value = ChatResponse("verified", metadata={"usage": {"total_tokens": 12}})
    return (
        GovernedLLM(delegate, governor, repository, "lease", Mock()),
        repository,
        governor,
        delegate,
    )


def test_every_model_call_is_reserved_journaled_and_cached():
    wrapper, repository, governor, delegate = gateway()
    response = wrapper.chat([{"role": "user", "content": "中文"}])
    assert response.content == "verified"
    repository.begin_call.assert_called_once()
    repository.finish_call.assert_called_once()
    governor.release.assert_called_once_with("permit", actual=12)
    repository.cached_call.return_value = response
    assert wrapper.chat([{"role": "user", "content": "中文"}]) == response
    delegate.chat.assert_called_once()
    repository.execution.return_value = {"open_model_calls": 1}
    with pytest.raises(RuntimeError):
        wrapper.chat([{"role": "user", "content": "retry unknown"}])
    delegate.chat.assert_called_once()


def test_only_explicit_429_retries_and_reserves_each_attempt(monkeypatch):
    wrapper, repository, governor, delegate = gateway()
    delegate.chat.side_effect = [
        LLMProviderError("429", status_code=429, retryable=True),
        ChatResponse("ok"),
    ]
    monkeypatch.setattr("multiuser.governor.time.sleep", lambda _: None)
    assert wrapper.chat([{"role": "user", "content": "question"}]).content == "ok"
    assert repository.begin_call.call_count == 2
    assert governor.acquire.call_count == 2
    assert governor.release.call_count == 2


@pytest.mark.parametrize("status", [None, 500, 503])
def test_uncertain_transport_does_not_retry_or_release_the_global_slot(status):
    wrapper, repository, governor, delegate = gateway()
    delegate.chat.side_effect = LLMProviderError("ambiguous", status_code=status, retryable=True)
    with pytest.raises(LLMProviderError):
        wrapper.chat([{"role": "user", "content": "question"}])
    delegate.chat.assert_called_once()
    governor.release.assert_not_called()
    repository.finish_call.assert_not_called()


def test_nested_provider_retry_is_forbidden():
    with pytest.raises(ValueError):
        gateway(Mock(max_retries=1, max_tokens=10))


@pytest.mark.parametrize(
    "url,token",
    [
        ("http://remote.invalid", "x" * 32),
        ("https://host.invalid/path", "x" * 32),
        ("https://host.invalid", "short"),
    ],
)
def test_internal_transport_rejects_untrusted_config(url, token):
    with pytest.raises(ValueError):
        ExecutionClient(url, token)


def test_queue_worker_rejects_arbitrary_function_before_import():
    worker = object.__new__(TrustedWorker)
    for job in (
        SimpleNamespace(func_name="os.system", args=("echo unsafe",), kwargs={}),
        SimpleNamespace(func_name="workers.runs.execute_run", args=("wrong",), kwargs={}),
        SimpleNamespace(
            func_name="workers.runs.execute_run",
            args=("run:" + "a" * 32,),
            kwargs={"endpoint": "untrusted"},
        ),
    ):
        with pytest.raises(ValueError):
            worker.perform_job(job, None)


def test_unused_model_slot_is_released_if_cancellation_precedes_provider():
    wrapper, repository, governor, delegate = gateway()
    wrapper.guard.side_effect = [None, AccessDenied("cancelled")]
    with pytest.raises(AccessDenied):
        wrapper.chat([{"role": "user", "content": "q"}])
    governor.release.assert_called_once_with("permit", actual=0)
    delegate.chat.assert_not_called()
    repository.begin_call.assert_not_called()


def test_unused_retrieval_slot_is_released_during_local_wait_cancellation():
    governor, semaphore = Mock(), Mock()
    governor.acquire.return_value = "permit"
    semaphore.acquire.return_value = False
    guard = Mock(side_effect=AccessDenied("cancelled"))
    delegate = Mock()
    knowledge = SharedKnowledge(delegate, guard, Mock(), semaphore, Mock(), "lease", governor)
    with pytest.raises(AccessDenied):
        knowledge.search("q", filters={})
    governor.release.assert_called_once_with("permit")
    semaphore.release.assert_not_called()
    delegate.search.assert_not_called()
