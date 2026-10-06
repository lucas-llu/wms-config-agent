"""Pure contracts, independent of model services or fixture credentials."""

from dataclasses import replace

import pytest

from api.runs import event_cursor
from multiuser.runs import RunLimits, RunRequest, public_run


@pytest.mark.parametrize(
    "field,value",
    [
        ("message", " "),
        ("message", None),
        ("message", "x" * 16001),
        ("idempotency_key", ""),
        ("idempotency_key", "x" * 129),
        ("idempotency_key", "a\nb"),
        ("idempotency_key", "a/b"),
        ("idempotency_key", "中文"),
        ("idempotency_key", None),
        ("expected_revision", 0),
        ("expected_revision", True),
        ("expected_revision", 1.0),
        ("answer_strategy", "admin"),
    ],
)
def test_invalid_request(field, value):
    with pytest.raises(ValueError):
        replace(RunRequest("中文 question", "key-001", 1), **{field: value})


def test_fingerprint_covers_exact_message_strategy_and_revision():
    request = RunRequest("配置", "key-a", 1)
    assert len(request.fingerprint) == 64
    assert replace(request, idempotency_key="key-b").fingerprint == request.fingerprint
    for change in ({"message": "配置 "}, {"expected_revision": 2}, {"answer_strategy": "review"}):
        assert replace(request, **change).fingerprint != request.fingerprint


@pytest.mark.parametrize(
    "field",
    [
        "pending_per_user",
        "running_per_user",
        "lease_seconds",
        "queue_seconds",
        "execution_seconds",
        "max_deliveries",
    ],
)
@pytest.mark.parametrize("value", [-1, 0, True, 1.0, 10000])
def test_limits_are_bounded_integers(field, value):
    with pytest.raises(ValueError):
        replace(RunLimits(), **{field: value})


def test_public_run_never_exposes_message_or_identity_metadata():
    row = {
        "run_id": "run:123",
        "conversation_id": "session:123",
        "status": "queued",
        "stage": "queued",
        "event_sequence": 1,
        "result_revision": None,
        "message": "PRIVATE",
        "owner_user_id": "PRIVATE",
        "content_hash": "PRIVATE",
    }
    result = public_run(row)
    assert "PRIVATE" not in str(result)
    assert result["events_url"] == "/v1/runs/run:123/events"


@pytest.mark.parametrize(
    "value", ["other/1", "run:123/-1", "1", "run:123/中文", "run:123/1/2", "run:123/" + "9" * 19]
)
def test_sse_cursor_is_run_bound(value):
    with pytest.raises(ValueError):
        event_cursor("run:123", value)


def test_sse_cursor_supports_zero_and_replay():
    assert event_cursor("run:123", "") == 0
    assert event_cursor("run:123", "run:123/7") == 7
