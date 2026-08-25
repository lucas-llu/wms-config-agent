"""Provider-neutral evaluation contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

MetricValue = float | int | None


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    metrics: dict[str, MetricValue]
    thresholds: dict[str, float] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    evaluator: str
    passed: bool
    metrics: dict[str, MetricValue]
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BaseEvaluator(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Return the stable provider name."""

    @abstractmethod
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        """Evaluate metrics without mutating the input request."""
