"""Deterministic SSE races without models, external identity or a database."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from api.runs import install_run_routes
from multiuser.access import AccessDenied
from multiuser.identity import InvalidIdentity
from multiuser.runs import RunLimits


def endpoint(monkeypatch, repo):
    app = FastAPI()
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = {"workspace_id": "fixture"}
    transaction = Mock()
    transaction.__enter__ = Mock(return_value=connection)
    transaction.__exit__ = Mock(return_value=False)
    application = SimpleNamespace(store=Mock())
    application.store.transaction.return_value = transaction
    monkeypatch.setattr("api.runs.RunRepository", lambda *a, **k: repo)
    install_run_routes(app, lambda: None, application, RunLimits())
    selected = next(r.endpoint for r in app.routes if r.path == "/v1/runs/{run_id}/events")
    request = Request({"type": "http", "headers": []})
    request.state.identity_check = Mock()
    return selected, request


def read(response):
    async def gather():
        return [part async for part in response.body_iterator]

    return asyncio.run(gather())


def test_terminal_event_committed_between_event_read_and_status_query_is_drained(monkeypatch):
    repo = Mock()
    final = {"run_id": "run:1", "sequence": 2, "status": "cancelled", "stage": "cancelled"}
    repo.events.side_effect = [[], [], [final]]  # preflight, empty batch, new terminal event
    repo.get.return_value = {"status": "cancelled", "sequence": 2}
    handler, request = endpoint(monkeypatch, repo)
    chunks = read(handler("run:1", request, None))
    assert len(chunks) == 1 and "run:1/2" in chunks[0]
    assert "cancelled" in chunks[0]


def test_terminal_history_over_one_page_is_fully_replayed(monkeypatch):
    repo = Mock()
    records = [
        {"run_id": "run:1", "sequence": i, "status": "running", "stage": "retrieving"}
        for i in range(1, 102)
    ]
    records[-1]["status"] = "succeeded"
    repo.events.side_effect = [records[:1], records[:100], records[100:]]
    repo.get.return_value = {"status": "succeeded", "sequence": 101}
    handler, request = endpoint(monkeypatch, repo)
    chunks = read(handler("run:1", request, None))
    assert len(chunks) == 101
    assert "run:1/101" in chunks[-1]


@pytest.mark.parametrize("error", [AccessDenied("revoked"), InvalidIdentity("expired")])
def test_stream_stops_without_private_data_after_revocation(monkeypatch, error):
    repo = Mock()
    repo.events.return_value = []
    handler, request = endpoint(monkeypatch, repo)
    request.state.identity_check.side_effect = error
    assert read(handler("run:1", request, None)) == []


def test_idle_heartbeat_does_not_submit_or_reexecute(monkeypatch):
    repo = Mock()
    repo.events.return_value = []
    repo.get.return_value = {"status": "queued", "sequence": 0}
    handler, request = endpoint(monkeypatch, repo)
    sleep = Mock()
    # Replace only this module's binding, never mutate asyncio's shared time module.
    monkeypatch.setattr(
        "api.runs.time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 21]), sleep=sleep)
    )
    assert read(handler("run:1", request, None)) == [": heartbeat\n\n"]
    sleep.assert_called_once_with(1)
    repo.submit.assert_not_called()
    repo.claim.assert_not_called()
