"""Append completed trace contexts to a local JSONL audit file."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from core.trace.trace_context import TraceContext


class TraceCollector:
    def __init__(self, path: str | Path, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self._lock = Lock()

    def start(
        self, trace_type: str, attributes: dict[str, Any] | None = None
    ) -> TraceContext | None:
        if not self.enabled:
            return None
        return TraceContext(trace_type, attributes)

    def collect(self, trace: TraceContext | None) -> None:
        if not self.enabled or trace is None:
            return
        serialized = json.dumps(trace.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.write("\n")
