"""Deterministic local metric-threshold evaluator."""

from __future__ import annotations

from libs.evaluator.base_evaluator import (
    BaseEvaluator,
    EvaluationRequest,
    EvaluationResult,
)


class ThresholdEvaluator(BaseEvaluator):
    @property
    def name(self) -> str:
        return "custom"

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        checks: dict[str, bool] = {}
        errors: list[str] = []
        for threshold_name, threshold in request.thresholds.items():
            if threshold_name.endswith("_min"):
                metric_name = threshold_name.removesuffix("_min")
                comparison = "min"
            elif threshold_name.endswith("_max"):
                metric_name = threshold_name.removesuffix("_max")
                comparison = "max"
            else:
                errors.append(f"Unsupported threshold: {threshold_name}")
                continue
            value = request.metrics.get(metric_name)
            if value is None:
                errors.append(f"Metric is unavailable: {metric_name}")
                checks[threshold_name] = False
                continue
            checks[threshold_name] = (
                float(value) >= threshold
                if comparison == "min"
                else float(value) <= threshold
            )
        return EvaluationResult(
            evaluator=self.name,
            passed=all(checks.values()) and not errors,
            metrics=dict(request.metrics),
            checks=checks,
            details={"thresholds": dict(request.thresholds)},
            errors=tuple(errors),
        )
