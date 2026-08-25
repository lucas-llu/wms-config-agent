"""Parallel composition of independent evaluation providers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from libs.evaluator.base_evaluator import (
    BaseEvaluator,
    EvaluationRequest,
    EvaluationResult,
)


class CompositeEvaluator(BaseEvaluator):
    def __init__(self, evaluators: list[BaseEvaluator]) -> None:
        if not evaluators:
            raise ValueError("evaluators must not be empty")
        names = [evaluator.name for evaluator in evaluators]
        if len(names) != len(set(names)):
            raise ValueError("evaluator names must be unique")
        self.evaluators = tuple(evaluators)

    @property
    def name(self) -> str:
        return "composite"

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        results: dict[str, EvaluationResult] = {}
        failures: list[str] = []
        with ThreadPoolExecutor(
            max_workers=len(self.evaluators), thread_name_prefix="evaluator"
        ) as pool:
            futures = {
                evaluator.name: pool.submit(evaluator.evaluate, request)
                for evaluator in self.evaluators
            }
            for name, future in futures.items():
                try:
                    results[name] = future.result()
                except Exception as exc:
                    failures.append(f"{name}: {type(exc).__name__}: {exc}")
        checks = {
            f"{name}.{check}": passed
            for name, result in sorted(results.items())
            for check, passed in sorted(result.checks.items())
        }
        errors = [
            f"{name}: {error}"
            for name, result in sorted(results.items())
            for error in result.errors
        ]
        errors.extend(failures)
        return EvaluationResult(
            evaluator=self.name,
            passed=(
                len(results) == len(self.evaluators)
                and all(result.passed for result in results.values())
                and not errors
            ),
            metrics=dict(request.metrics),
            checks=checks,
            details={
                "evaluators": {name: result.to_dict() for name, result in sorted(results.items())}
            },
            errors=tuple(errors),
        )
