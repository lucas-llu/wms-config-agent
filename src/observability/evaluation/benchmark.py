"""Validated, privacy-aware contracts for retrieval benchmark datasets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any


class BenchmarkValidationError(ValueError):
    """Raised when a benchmark cannot provide reliable ground truth."""


@dataclass(frozen=True, slots=True)
class BenchmarkExpectation:
    process_codes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()
    should_refuse: bool = False

    def __post_init__(self) -> None:
        positive_labels = (
            self.process_codes,
            self.sources,
            self.domains,
            self.document_types,
        )
        if self.should_refuse and any(positive_labels):
            raise BenchmarkValidationError(
                "A refusal case cannot also define positive relevance labels"
            )
        if not self.should_refuse and not any(positive_labels):
            raise BenchmarkValidationError(
                "A positive case must define at least one relevance label"
        )
        for source in self.sources:
            if (
                Path(source).is_absolute()
                or PureWindowsPath(source).is_absolute()
                or ".." in PurePath(source).parts
                or ".." in PureWindowsPath(source).parts
            ):
                raise BenchmarkValidationError(
                    "Benchmark sources must be relative paths or sanitized IDs"
                )

    @classmethod
    def from_dict(cls, payload: Any) -> BenchmarkExpectation:
        if not isinstance(payload, dict):
            raise BenchmarkValidationError("expected must be an object")
        allowed = {
            "process_codes",
            "sources",
            "domains",
            "document_types",
            "should_refuse",
        }
        _reject_unknown(payload, allowed, "expected")
        should_refuse = payload.get("should_refuse", False)
        if not isinstance(should_refuse, bool):
            raise BenchmarkValidationError("expected.should_refuse must be a boolean")
        return cls(
            process_codes=_string_tuple(payload.get("process_codes", []), "process_codes"),
            sources=_string_tuple(payload.get("sources", []), "sources"),
            domains=_string_tuple(payload.get("domains", []), "domains"),
            document_types=_string_tuple(
                payload.get("document_types", []), "document_types"
            ),
            should_refuse=should_refuse,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    category: str
    query: str
    expected: BenchmarkExpectation
    filters: dict[str, str | int | float | bool] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Any) -> BenchmarkCase:
        if not isinstance(payload, dict):
            raise BenchmarkValidationError("Each test case must be an object")
        allowed = {"id", "category", "query", "expected", "filters"}
        _reject_unknown(payload, allowed, "test case")
        case_id = _required_string(payload, "id")
        category = _required_string(payload, "category")
        query = _required_string(payload, "query")
        filters = payload.get("filters", {})
        if not isinstance(filters, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, str | int | float | bool)
            for key, value in filters.items()
        ):
            raise BenchmarkValidationError("filters must contain scalar values")
        supported_filters = {
            "collection",
            "domain",
            "module",
            "document_type",
            "doc_type",
            "process_code",
            "process_stage",
            "site",
            "environment",
            "version",
        }
        unknown_filters = set(filters) - supported_filters
        if unknown_filters:
            raise BenchmarkValidationError(
                f"Unsupported filters: {', '.join(sorted(unknown_filters))}"
            )
        return cls(
            case_id=case_id,
            category=category,
            query=query,
            expected=BenchmarkExpectation.from_dict(payload.get("expected")),
            filters=dict(filters),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkDataset:
    name: str
    description: str
    test_cases: tuple[BenchmarkCase, ...]
    thresholds: dict[str, float]
    schema_version: int = 1

    @classmethod
    def load(cls, path: str | Path) -> BenchmarkDataset:
        dataset_path = Path(path)
        try:
            payload = json.loads(dataset_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BenchmarkValidationError(
                f"Benchmark dataset does not exist: {dataset_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise BenchmarkValidationError(f"Invalid benchmark JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BenchmarkValidationError("Benchmark root must be an object")
        allowed = {
            "schema_version",
            "name",
            "description",
            "thresholds",
            "test_cases",
        }
        _reject_unknown(payload, allowed, "benchmark")
        if payload.get("schema_version") != 1:
            raise BenchmarkValidationError("Only benchmark schema_version 1 is supported")
        cases_payload = payload.get("test_cases")
        if not isinstance(cases_payload, list) or not cases_payload:
            raise BenchmarkValidationError("test_cases must be a non-empty array")
        test_cases = tuple(BenchmarkCase.from_dict(case) for case in cases_payload)
        identifiers = [case.case_id for case in test_cases]
        if len(identifiers) != len(set(identifiers)):
            raise BenchmarkValidationError("Benchmark case IDs must be unique")
        thresholds = _thresholds(payload.get("thresholds", {}))
        return cls(
            name=_required_string(payload, "name"),
            description=_required_string(payload, "description"),
            test_cases=test_cases,
            thresholds=thresholds,
        )

    @property
    def fingerprint(self) -> str:
        serialized = json.dumps(
            self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_THRESHOLD_NAMES = {
    "hit_at_1_min",
    "hit_at_3_min",
    "hit_at_5_min",
    "mrr_at_5_min",
    "refusal_accuracy_min",
    "evidence_accuracy_min",
    "p95_latency_ms_max",
}


def _thresholds(payload: Any) -> dict[str, float]:
    if not isinstance(payload, dict):
        raise BenchmarkValidationError("thresholds must be an object")
    _reject_unknown(payload, _THRESHOLD_NAMES, "thresholds")
    result: dict[str, float] = {}
    for name, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise BenchmarkValidationError(f"Threshold {name} must be non-negative")
        if not name.endswith("_ms_max") and value > 1:
            raise BenchmarkValidationError(f"Threshold {name} must be between 0 and 1")
        result[name] = float(value)
    return result


def _string_tuple(payload: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(payload, list) or any(
        not isinstance(value, str) or not value.strip() for value in payload
    ):
        raise BenchmarkValidationError(f"expected.{field_name} must be a string array")
    return tuple(dict.fromkeys(value.strip() for value in payload))


def _required_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _reject_unknown(payload: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise BenchmarkValidationError(
            f"Unknown {label} fields: {', '.join(sorted(unknown))}"
        )
