from __future__ import annotations

from pathlib import Path

import pytest

from core.settings import EvaluationSettings
from libs.evaluator import (
    BaseEvaluator,
    CompositeEvaluator,
    EvaluationRequest,
    EvaluationResult,
    EvaluatorFactory,
    ThresholdEvaluator,
)


class StaticEvaluator(BaseEvaluator):
    def __init__(self, name: str, passed: bool = True) -> None:
        self._name = name
        self._passed = passed

    @property
    def name(self) -> str:
        return self._name

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        return EvaluationResult(
            evaluator=self.name,
            passed=self._passed,
            metrics=request.metrics,
            checks={"ok": self._passed},
        )


class BrokenEvaluator(StaticEvaluator):
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        del request
        raise RuntimeError("provider failed")


def test_threshold_evaluator_checks_minimum_and_maximum() -> None:
    result = ThresholdEvaluator().evaluate(
        EvaluationRequest(
            metrics={"hit_at_3": 0.9, "p95_latency_ms": 100.0},
            thresholds={"hit_at_3_min": 0.85, "p95_latency_ms_max": 200.0},
        )
    )

    assert result.passed is True
    assert result.checks == {
        "hit_at_3_min": True,
        "p95_latency_ms_max": True,
    }


def test_threshold_evaluator_reports_unknown_threshold_and_missing_metric() -> None:
    result = ThresholdEvaluator().evaluate(
        EvaluationRequest(
            metrics={},
            thresholds={"hit_at_3_min": 0.8, "unsupported": 1.0},
        )
    )

    assert result.passed is False
    assert result.checks["hit_at_3_min"] is False
    assert "Metric is unavailable: hit_at_3" in result.errors
    assert "Unsupported threshold: unsupported" in result.errors


def test_composite_evaluator_isolates_provider_failure() -> None:
    evaluator = CompositeEvaluator(
        [StaticEvaluator("healthy"), BrokenEvaluator("broken")]
    )

    result = evaluator.evaluate(EvaluationRequest(metrics={"score": 1.0}))

    assert result.passed is False
    assert result.checks["healthy.ok"] is True
    assert "broken: RuntimeError: provider failed" in result.errors


def test_evaluator_factory_creates_custom_and_rejects_unknown() -> None:
    settings = EvaluationSettings(
        backends=("custom",), golden_test_set=Path("golden")
    )

    assert isinstance(EvaluatorFactory.create(settings), ThresholdEvaluator)
    with pytest.raises(ValueError, match="Unknown evaluator provider"):
        EvaluatorFactory.create(
            EvaluationSettings(
                backends=("missing",), golden_test_set=settings.golden_test_set
            )
        )
    with pytest.raises(ValueError, match="At least one evaluator"):
        EvaluatorFactory.create(
            EvaluationSettings(backends=(), golden_test_set=settings.golden_test_set)
        )


def test_evaluator_factory_registers_and_composes_providers() -> None:
    provider = "test-static-provider"
    EvaluatorFactory.register(provider, lambda settings: StaticEvaluator(provider))

    evaluator = EvaluatorFactory.create(
        EvaluationSettings(
            backends=("custom", provider), golden_test_set=Path("golden")
        )
    )
    result = evaluator.evaluate(
        EvaluationRequest(metrics={"hit_at_3": 1.0}, thresholds={"hit_at_3_min": 1.0})
    )

    assert isinstance(evaluator, CompositeEvaluator)
    assert result.passed is True
    assert set(result.details["evaluators"]) == {"custom", provider}
    with pytest.raises(ValueError, match="already registered"):
        EvaluatorFactory.register(provider, lambda settings: StaticEvaluator(provider))
    with pytest.raises(ValueError, match="non-empty"):
        EvaluatorFactory.register("", lambda settings: StaticEvaluator("empty"))
