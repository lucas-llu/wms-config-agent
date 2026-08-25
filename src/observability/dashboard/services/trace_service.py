"""Bounded, tolerant, and privacy-safe JSONL trace reader for Dashboard views."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|password|secret|access[_-]?token)", re.I)
_BODY_KEY = re.compile(r"(?:^|_)(?:body|content|document_text|prompt|raw_text|text)(?:$|_)", re.I)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{12,}|"
    r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+)"
)
_ALLOWED_TRACE_TYPES = {"ingestion", "query"}


@dataclass(frozen=True, slots=True)
class TraceStage:
    name: str
    elapsed_ms: float
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TraceRecord:
    trace_id: str
    trace_type: str
    started_at: str
    finished_at: str | None
    total_elapsed_ms: float
    status: str
    attributes: dict[str, Any]
    stages: tuple[TraceStage, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TraceReadResult:
    records: tuple[TraceRecord, ...]
    malformed_lines: int
    truncated: bool


class TraceService:
    """Read recent traces without failing a page because one JSONL line is incomplete."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_records: int = 500,
        max_read_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        if max_records <= 0 or max_read_bytes <= 0:
            raise ValueError("trace reader bounds must be greater than 0")
        self.path = Path(path)
        self.max_records = max_records
        self.max_read_bytes = max_read_bytes

    def list_traces(
        self,
        trace_type: str,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> TraceReadResult:
        if trace_type not in _ALLOWED_TRACE_TYPES:
            raise ValueError(f"Unsupported trace type: {trace_type}")
        if limit <= 0:
            raise ValueError("limit must be greater than 0")
        records, malformed, truncated = self._read_recent()
        needle = (search or "").strip().casefold()
        selected = [
            record
            for record in records
            if record.trace_type == trace_type
            and (status is None or record.status == status)
            and (not needle or needle in self._searchable_text(record))
        ]
        selected.sort(key=lambda item: (item.started_at, item.trace_id), reverse=True)
        return TraceReadResult(tuple(selected[:limit]), malformed, truncated)

    @staticmethod
    def summary_rows(records: tuple[TraceRecord, ...]) -> list[dict[str, Any]]:
        return [
            {
                "Trace ID": record.trace_id,
                "Started": record.started_at,
                "Status": record.status,
                "Duration (ms)": record.total_elapsed_ms,
                "Collection": str(record.attributes.get("collection", "")),
                "Subject": str(
                    record.attributes.get("source_name") or record.attributes.get("query") or ""
                ),
            }
            for record in records
        ]

    @staticmethod
    def stage_rows(record: TraceRecord) -> list[dict[str, Any]]:
        return [
            {
                "Stage": stage.name,
                "Duration (ms)": stage.elapsed_ms,
                "Status": str(stage.details.get("status", "ok")),
                "Method": TraceService._display_value(stage.details.get("method")),
                "Provider": TraceService._display_value(stage.details.get("provider")),
                "Records": stage.details.get(
                    "result_count",
                    stage.details.get("record_count", stage.details.get("chunk_count", "")),
                ),
            }
            for stage in record.stages
        ]

    @staticmethod
    def query_diagnostics(record: TraceRecord) -> dict[str, Any]:
        stages = {stage.name: stage for stage in record.stages}
        dense = stages.get("dense_retrieval")
        sparse = stages.get("sparse_retrieval")
        fusion = stages.get("fusion")
        rerank = stages.get("rerank")
        fusion_details = fusion.details if fusion else {}
        rankings = fusion_details.get("rankings", {})
        return {
            "dense_count": TraceService._result_count(dense),
            "sparse_count": TraceService._result_count(sparse),
            "final_count": TraceService._safe_count(fusion_details.get("result_count")),
            "rerank_fallback": bool(rerank and rerank.details.get("fallback_used")),
            "failures": [
                stage.name
                for stage in record.stages
                if stage.details.get("status") == "error" or stage.name.endswith("_failure")
            ],
            "rankings": rankings if isinstance(rankings, dict) else {},
        }

    def _read_recent(self) -> tuple[list[TraceRecord], int, bool]:
        if not self.path.is_file():
            return [], 0, False
        records: deque[TraceRecord] = deque(maxlen=self.max_records)
        malformed = 0
        with self.path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - self.max_read_bytes)
            truncated = start > 0
            stream.seek(start)
            if truncated:
                stream.readline()
            for raw_line in stream:
                try:
                    payload = json.loads(raw_line.decode("utf-8"))
                    record = self._parse_record(payload)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    malformed += 1
                    continue
                records.append(record)
        return list(records), malformed, truncated

    @staticmethod
    def _parse_record(payload: Any) -> TraceRecord:
        if not isinstance(payload, dict):
            raise ValueError("Trace line must be an object")
        trace_id = TraceService._required_string(payload, "trace_id")
        trace_type = TraceService._required_string(payload, "trace_type")
        if trace_type not in _ALLOWED_TRACE_TYPES:
            raise ValueError("Unsupported trace type")
        started_at = TraceService._required_string(payload, "started_at")
        attributes = TraceService._safe_mapping(payload.get("attributes", {}))
        raw_stages = payload.get("stages", [])
        if not isinstance(raw_stages, list):
            raise ValueError("Trace stages must be a list")
        stages: list[TraceStage] = []
        for raw_stage in raw_stages:
            if not isinstance(raw_stage, dict):
                raise ValueError("Trace stage must be an object")
            stages.append(
                TraceStage(
                    TraceService._required_string(raw_stage, "name"),
                    TraceService._number(raw_stage.get("elapsed_ms", 0.0)),
                    TraceService._safe_mapping(raw_stage.get("details", {})),
                )
            )
        finished_at = payload.get("finished_at")
        error = payload.get("error")
        return TraceRecord(
            trace_id=trace_id,
            trace_type=trace_type,
            started_at=started_at,
            finished_at=str(finished_at) if isinstance(finished_at, str) else None,
            total_elapsed_ms=TraceService._number(payload.get("total_elapsed_ms", 0.0)),
            status=str(payload.get("status") or "incomplete"),
            attributes=attributes,
            stages=tuple(stages),
            error=str(error) if isinstance(error, str) else None,
        )

    @staticmethod
    def _safe_mapping(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): TraceService._safe_value(item, key=str(key))
            for key, item in value.items()
            if not _SECRET_KEY.search(str(key)) and not _BODY_KEY.search(str(key))
        }

    @staticmethod
    def _safe_value(value: Any, *, key: str | None = None) -> Any:
        if key and (_SECRET_KEY.search(key) or _BODY_KEY.search(key)):
            return "[REDACTED]"
        if isinstance(value, dict):
            return TraceService._safe_mapping(value)
        if isinstance(value, list):
            return [TraceService._safe_value(item) for item in value]
        if value is None or isinstance(value, bool | int | float):
            return value
        text = _SECRET_VALUE.sub("[REDACTED]", str(value))
        return text if len(text) <= 500 else text[:497] + "..."

    @staticmethod
    def _searchable_text(record: TraceRecord) -> str:
        values = [record.trace_id, record.status]
        for key in ("source_name", "collection", "query"):
            value = record.attributes.get(key)
            if isinstance(value, str):
                values.append(value)
        return " ".join(values).casefold()

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Trace {key} must be a non-empty string")
        return value

    @staticmethod
    def _number(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("Trace duration must be numeric")
        return round(max(float(value), 0.0), 3)

    @staticmethod
    def _display_value(value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return "" if value is None else str(value)

    @staticmethod
    def _result_count(stage: TraceStage | None) -> int:
        if stage is None:
            return 0
        return TraceService._safe_count(stage.details.get("result_count"))

    @staticmethod
    def _safe_count(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return 0
        return max(int(value), 0)
