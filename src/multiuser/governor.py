"""Durable global model permits; Redis loss cannot reset an in-flight budget."""

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass

from libs.llm.openai_compatible_llm import LLMProviderError


class ModelBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelLimits:
    inflight: int = 4
    review_inflight: int = 1
    rpm: int = 60
    tpm: int = 200000
    wait_seconds: int = 60
    hold_seconds: int = 900
    retries: int = 2

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or not 0 <= value <= 100000000:
                raise ValueError("Invalid model governor limits")
            if value == 0 and name not in {"review_inflight", "retries"}:
                raise ValueError("Positive model governor limit required")
        if self.review_inflight > self.inflight or self.retries > 2:
            raise ValueError("Invalid model sublimit/retry limit")


class ModelGovernor:
    def __init__(self, control, provider_key, *, limits=None):
        self.control, self.key, self.limits = control, provider_key, limits or ModelLimits()
        if not provider_key or len(provider_key) > 128:
            raise ValueError("Stable provider/model identity required")
        config = json.dumps(asdict(self.limits), sort_keys=True)
        with control.transaction() as connection:
            connection.execute(
                "INSERT INTO model_governors VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (self.key, config),
            )
            row = connection.execute(
                "SELECT config_json FROM model_governors WHERE provider_key=%s", (self.key,)
            ).fetchone()
            if row["config_json"] != config:
                raise ValueError("All execution replicas must use identical model limits")

    def reserve(self, tokens, *, review=False):
        if type(tokens) is not int or tokens < 1 or tokens > self.limits.tpm:
            raise ModelBusy("Single call exceeds configured token window")
        with self.control.transaction() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,9134202))", (self.key,)
            )
            row = connection.execute(
                "SELECT count(*) FILTER(WHERE NOT released AND "
                "expires_at>clock_timestamp()) AS active,"
                "count(*) FILTER(WHERE review AND NOT released AND "
                "expires_at>clock_timestamp()) AS reviews,"
                "count(*) FILTER(WHERE issued_at>clock_timestamp()-interval '60 seconds') AS rpm,"
                "coalesce(sum(token_reservation) FILTER(WHERE "
                "issued_at>clock_timestamp()-interval '60 seconds'),0) AS tpm "
                "FROM model_permits WHERE provider_key=%s AND "
                "(issued_at>clock_timestamp()-interval '60 seconds' "
                "OR expires_at>clock_timestamp())",
                (self.key,),
            ).fetchone()
            if (
                row["active"] >= self.limits.inflight
                or (review and row["reviews"] >= self.limits.review_inflight)
                or row["rpm"] >= self.limits.rpm
                or row["tpm"] + tokens > self.limits.tpm
            ):
                return None
            permit = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO model_permits(permit_id,provider_key,review,"
                "token_reservation,expires_at) "
                "VALUES(%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second'))",
                (permit, self.key, review, tokens, self.limits.hold_seconds),
            )
            # Bounded metadata maintenance, not a full-keyspace Redis scan.
            connection.execute(
                "DELETE FROM model_permits WHERE permit_id IN(SELECT permit_id FROM model_permits "
                "WHERE released AND expires_at<clock_timestamp() AND "
                "issued_at<clock_timestamp()-interval '60 seconds' ORDER BY issued_at LIMIT 50)"
            )
            return permit

    def acquire(self, tokens, guard, *, review=False):
        deadline = time.monotonic() + self.limits.wait_seconds
        while True:
            guard()
            if permit := self.reserve(tokens, review=review):
                return permit
            if time.monotonic() >= deadline:
                raise ModelBusy("Model capacity wait exceeded")
            time.sleep(0.2)

    def release(self, permit, *, actual=None):
        with self.control.transaction() as connection:
            if type(actual) is int and actual >= 0:
                connection.execute(
                    "UPDATE model_permits SET released=true,token_reservation=%s "
                    "WHERE permit_id=%s",
                    (actual, permit),
                )
            else:
                connection.execute(
                    "UPDATE model_permits SET released=true WHERE permit_id=%s", (permit,)
                )


class GovernedLLM:
    def __init__(self, delegate, governor, repository, lease, guard, *, review=False):
        self.delegate, self.governor, self.repository, self.lease, self.guard = (
            delegate,
            governor,
            repository,
            lease,
            guard,
        )
        self.review = review
        if getattr(delegate, "max_retries", 0) != 0:
            raise ValueError("Provider retries must be zero beneath the global gateway")

    def chat(self, messages, trace=None):
        self.guard()
        if self.repository.execution(self.lease)["open_model_calls"]:
            raise RuntimeError("Unknown previous provider outcome; no further model calls")
        key = hashlib.sha256(
            json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if response := self.repository.cached_call(self.lease, key):
            return response
        # UTF-8 bytes plus framing conservatively bound input; reserve max output, not context size.
        tokens = sum(len(m["content"].encode()) + 32 for m in messages) + getattr(
            self.delegate, "max_tokens", 4096
        )
        for retry in range(self.governor.limits.retries + 1):
            permit = self.governor.acquire(tokens, self.guard, review=self.review)
            self.guard()
            attempt = self.repository.begin_call(self.lease, key)
            self.repository.progress(self.lease, "generating")
            try:
                response = self.delegate.chat(messages, trace=trace)
            except LLMProviderError as exc:
                # Only explicit 429 is a definite rejection. Timeouts/5xx stay unknown.
                if exc.status_code == 429:
                    self.repository.finish_call(self.lease, key, attempt, rejected=True)
                    self.governor.release(permit, actual=0)
                    if retry < self.governor.limits.retries:
                        self.guard()
                        time.sleep(min(0.25 * 2**retry, 1))
                        continue
                raise
            self.guard()
            self.repository.finish_call(self.lease, key, attempt, response)
            usage = response.metadata.get("usage", {})
            actual = usage.get("total_tokens") if isinstance(usage, dict) else None
            self.governor.release(permit, actual=actual)
            return response
        raise AssertionError("unreachable model retry state")
