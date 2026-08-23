"""Thread-safe timing context for query and ingestion traces."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from threading import Lock
from typing import Any


class TraceContext:
    def __init__(
        self,
        trace_type: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.trace_id = uuid.uuid4().hex
        self.trace_type = trace_type
        self.started_at = datetime.now(UTC).isoformat()
        self.attributes = dict(attributes or {})
        self._started = time.perf_counter()
        self._lock = Lock()
        self._stages: list[dict[str, Any]] = []
        self._finished: dict[str, Any] | None = None

    def record_stage(
        self,
        name: str,
        elapsed_ms: float,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        stage: dict[str, Any] = {
            "name": name,
            "elapsed_ms": round(elapsed_ms, 3),
        }
        if details:
            stage["details"] = details
        with self._lock:
            self._stages.append(stage)

    def finish(self, *, status: str = "ok", error: str | None = None) -> None:
        finished: dict[str, Any] = {
            "finished_at": datetime.now(UTC).isoformat(),
            "total_elapsed_ms": round((time.perf_counter() - self._started) * 1000, 3),
            "status": status,
        }
        if error:
            finished["error"] = error
        with self._lock:
            if self._finished is None:
                self._finished = finished

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            stages = [dict(stage) for stage in self._stages]
            finished = dict(self._finished) if self._finished else None
        payload: dict[str, Any] = {
            "trace_id": self.trace_id,
            "trace_type": self.trace_type,
            "started_at": self.started_at,
            "attributes": self.attributes,
            "stages": stages,
        }
        if finished:
            payload.update(finished)
        return payload
