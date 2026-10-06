"""Durable-run contracts. No provider text, credentials or queue-library state."""

import hashlib
import json
from dataclasses import dataclass

TERMINAL = frozenset(
    {
        "succeeded",
        "cancelled",
        "failed",
        "uncertain",
        "timed_out",
        "authorization_required",
        "recovery_required",
    }
)
STAGES = frozenset({"queued", "retrieving", "generating", "validating", "persisting"})


class RunConflict(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class RunBusy(RunConflict):
    pass


class LostLease(Exception):
    """Cancellation, an expired lease or a newer execution fences this writer."""


@dataclass(frozen=True)
class RunLimits:
    pending_per_user: int = 5
    running_per_user: int = 2
    lease_seconds: int = 45
    queue_seconds: int = 600
    execution_seconds: int = 600
    max_deliveries: int = 5

    def __post_init__(self):
        bounds = {
            "pending_per_user": (1, 100),
            "running_per_user": (1, 20),
            "lease_seconds": (5, 120),
            "queue_seconds": (5, 3600),
            "execution_seconds": (5, 3600),
            "max_deliveries": (1, 20),
        }
        for key, (low, high) in bounds.items():
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid bounded run limits")


@dataclass(frozen=True)
class RunRequest:
    message: str
    idempotency_key: str
    expected_revision: int
    answer_strategy: str = "standard"

    def __post_init__(self):
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Message required")
        if len(self.message) > 16000:
            raise ValueError("Message too long")
        key = self.idempotency_key
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 128
            or any(not (c.isascii() and (c.isalnum() or c in "-_")) for c in key)
        ):
            raise ValueError("Invalid idempotency key")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("Positive revision required")
        if self.answer_strategy not in {"standard", "review"}:
            raise ValueError("Invalid answer strategy")

    @property
    def fingerprint(self):
        value = json.dumps(
            [self.message, self.answer_strategy, self.expected_revision],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class RunLease:
    run_id: str
    conversation_id: str
    epoch: int
    checkpoint_thread: str


def public_run(row):
    """Keep request contents, session metadata and checkpoint identifiers private."""
    return {
        "run_id": row["run_id"],
        "conversation_id": row["conversation_id"],
        "status": row["status"],
        "stage": row["stage"],
        "sequence": row["event_sequence"],
        "result_revision": row["result_revision"],
        "events_url": f"/v1/runs/{row['run_id']}/events",
    }
