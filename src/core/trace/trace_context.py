"""Thread-safe timing context for query and ingestion traces."""

from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime
from threading import Lock
from typing import Any

_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|password|secret|access[_-]?token)", re.I)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{12,}|"
    r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+)"
)
_DOCUMENT_TEXT_KEYS = {"content", "document_text", "prompt", "raw_text", "text"}
_ERROR_CATEGORY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


class TraceContext:
    def __init__(
        self,
        trace_type: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.trace_id = uuid.uuid4().hex
        self.trace_type = trace_type
        self.started_at = datetime.now(UTC).isoformat()
        self.attributes = _sanitize_trace_value(dict(attributes or {}))
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
            stage["details"] = _sanitize_trace_value(details)
        with self._lock:
            self._stages.append(stage)

    def finish(self, *, status: str = "ok", error: str | None = None) -> None:
        finished: dict[str, Any] = {
            "finished_at": datetime.now(UTC).isoformat(),
            "total_elapsed_ms": round((time.perf_counter() - self._started) * 1000, 3),
            "status": status,
        }
        if error:
            finished["error"] = _sanitize_trace_error(error)
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


def _sanitize_trace_value(value: Any, *, key: str | None = None) -> Any:
    """Keep traces diagnostic while excluding credentials and document/prompt bodies."""
    if key is not None and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if key is not None and key.casefold() in _DOCUMENT_TEXT_KEYS:
        return "[OMITTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_trace_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_sanitize_trace_value(item) for item in value]
    if isinstance(value, str):
        sanitized = _SECRET_VALUE.sub("[REDACTED]", value)
        return sanitized if len(sanitized) <= 1000 else sanitized[:997] + "..."
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)


def _sanitize_trace_error(error: str) -> str:
    """Retain an exception category, never an arbitrary provider/document error body."""

    sanitized = _sanitize_trace_value(error)
    if isinstance(sanitized, str) and _ERROR_CATEGORY.fullmatch(sanitized):
        return sanitized
    return "[REDACTED]"
