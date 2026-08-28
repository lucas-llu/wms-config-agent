"""Structured requirement extraction and minimal clarification questions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.contracts import Assumption, OpenQuestion, stable_contract_id
from agents.llm_json import StructuredLLMError, invoke_json
from libs.llm import BaseLLM

_LIST_FIELDS = frozenset({"modules", "integrations", "customizations", "constraints"})
_SCALAR_FIELDS = frozenset(
    {"product_version", "site", "environment", "business_process", "volume_profile"}
)
_REQUIRED_FIELDS = ("business_process", "modules", "product_version", "site", "environment")
_QUESTIONS = {
    "business_process": "Which WMS business process should this configuration support?",
    "modules": "Which WMS/JDA modules are in scope?",
    "product_version": "Which product version is the target environment running?",
    "site": "Which warehouse or site will use this configuration?",
    "environment": "Which environment is targeted (development, test, or production)?",
}


@dataclass(frozen=True, slots=True)
class RequirementExtraction:
    confirmed_context: dict[str, Any]
    assumptions: tuple[Assumption, ...]
    open_questions: tuple[OpenQuestion, ...]
    summary: str
    retries: int
    tokens_used: int


class RequirementAgent:
    def __init__(
        self,
        llm: BaseLLM,
        *,
        max_retries: int,
        max_questions: int,
        prompt_path: str | Path,
    ) -> None:
        if max_questions <= 0:
            raise ValueError("max_questions must be greater than 0")
        self.llm = llm
        self.max_retries = max_retries
        self.max_questions = max_questions
        self.prompt = Path(prompt_path).read_text(encoding="utf-8")

    def extract(
        self,
        *,
        user_message: str,
        turn_id: str,
        confirmed_context: dict[str, Any],
        recent_turns: list[dict[str, str]],
        requirement_summary: str = "",
    ) -> RequirementExtraction:
        invocation = invoke_json(
            self.llm,
            [
                {
                    "role": "user",
                    "content": self.prompt.replace(
                        "{confirmed_context}",
                        json.dumps(confirmed_context, ensure_ascii=False, sort_keys=True),
                    )
                    .replace("{recent_turns}", json.dumps(recent_turns, ensure_ascii=False))
                    .replace("{requirement_summary}", requirement_summary)
                    .replace("{user_message}", user_message),
                }
            ],
            max_retries=self.max_retries,
            validator=self._validate_payload,
        )
        payload = invocation.payload
        try:
            extracted = payload.get("confirmed_context", {})
            if not isinstance(extracted, dict):
                raise ValueError("confirmed_context must be a JSON object")
            merged = _merge_confirmed_context(confirmed_context, extracted)
            assumptions = _assumptions(payload.get("assumptions", []), turn_id)
            questions = _missing_questions(merged, self.max_questions)
            summary_value = payload.get("summary")
            summary = (
                summary_value.strip()
                if isinstance(summary_value, str) and summary_value.strip()
                else user_message.strip()
            )
        except ValueError as exc:
            raise StructuredLLMError(
                str(exc), retries=invocation.retries, tokens_used=invocation.tokens_used
            ) from exc
        return RequirementExtraction(
            merged,
            assumptions,
            questions,
            summary,
            invocation.retries,
            invocation.tokens_used,
        )

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> None:
        context = payload.get("confirmed_context", {})
        if not isinstance(context, dict):
            raise ValueError("confirmed_context must be a JSON object")
        for field in _LIST_FIELDS:
            value = context.get(field)
            if value is not None and (
                not isinstance(value, list) or any(not isinstance(item, str) for item in value)
            ):
                raise ValueError(f"confirmed_context.{field} must be a list of strings")
        assumptions = payload.get("assumptions", [])
        if not isinstance(assumptions, list) or any(
            not isinstance(item, str) for item in assumptions
        ):
            raise ValueError("assumptions must be a list of strings")
        summary = payload.get("summary", "")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("summary must be a string")


def _merge_confirmed_context(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for field in _SCALAR_FIELDS:
        value = incoming.get(field)
        if isinstance(value, str) and value.strip():
            merged[field] = value.strip()
    for field in _LIST_FIELDS:
        value = incoming.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"confirmed_context.{field} must be a list of strings")
        existing = merged.get(field, [])
        if not isinstance(existing, list):
            existing = list(existing) if isinstance(existing, tuple) else []
        merged[field] = list(
            dict.fromkeys([*existing, *(item.strip() for item in value if item.strip())])
        )
    return merged


def _assumptions(values: Any, turn_id: str) -> tuple[Assumption, ...]:
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ValueError("assumptions must be a list of strings")
    return tuple(
        Assumption(
            assumption_id=stable_contract_id("assumption", {"text": text, "turn": turn_id}),
            text=text,
            source_turn_id=turn_id,
            confirmed=False,
        )
        for text in (item.strip() for item in values)
        if text
    )


def _missing_questions(context: dict[str, Any], max_questions: int) -> tuple[OpenQuestion, ...]:
    missing: list[str] = []
    for field in _REQUIRED_FIELDS:
        value = context.get(field)
        if value is None or value == "" or value == []:
            missing.append(field)
    return tuple(
        OpenQuestion(
            question_id=stable_contract_id("question", {"field": field}),
            text=_QUESTIONS[field],
            reason=f"required_context_missing:{field}",
        )
        for field in missing[:max_questions]
    )
