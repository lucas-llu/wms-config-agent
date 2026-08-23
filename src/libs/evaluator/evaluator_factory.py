"""Configuration-driven evaluator factory."""

from __future__ import annotations

from collections.abc import Callable

from core.settings import EvaluationSettings, Settings
from libs.evaluator.base_evaluator import BaseEvaluator
from libs.evaluator.composite_evaluator import CompositeEvaluator
from libs.evaluator.threshold_evaluator import ThresholdEvaluator

EvaluatorBuilder = Callable[[EvaluationSettings], BaseEvaluator]


class EvaluatorFactory:
    _providers: dict[str, EvaluatorBuilder] = {
        "custom": lambda settings: ThresholdEvaluator()
    }

    @classmethod
    def create(cls, settings: Settings | EvaluationSettings) -> BaseEvaluator:
        evaluation_settings = (
            settings.evaluation if isinstance(settings, Settings) else settings
        )
        if not evaluation_settings.backends:
            raise ValueError("At least one evaluator backend must be configured")
        evaluators: list[BaseEvaluator] = []
        for provider in evaluation_settings.backends:
            try:
                builder = cls._providers[provider]
            except KeyError as exc:
                supported = ", ".join(sorted(cls._providers))
                raise ValueError(
                    f"Unknown evaluator provider '{provider}'; "
                    f"supported providers: {supported}"
                ) from exc
            evaluators.append(builder(evaluation_settings))
        if len(evaluators) == 1:
            return evaluators[0]
        return CompositeEvaluator(evaluators)

    @classmethod
    def register(
        cls,
        provider: str,
        builder: EvaluatorBuilder,
        *,
        replace: bool = False,
    ) -> None:
        if not provider.strip():
            raise ValueError("provider must be a non-empty string")
        if provider in cls._providers and not replace:
            raise ValueError(f"Evaluator provider is already registered: {provider}")
        cls._providers[provider] = builder
