"""Rule-first, structured-LLM-fallback intent classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agents.contracts import IntentType
from agents.llm_json import JSONInvocation, StructuredLLMError, invoke_json
from libs.llm import BaseLLM

_INSPECT = re.compile(r"\b(show|view|inspect|explain)\b.*\b(draft|plan)\b|草案|当前方案", re.I)
_UNSUPPORTED = re.compile(r"\b(weather|stock|joke|music|recipe)\b|天气|股票|笑话|音乐|菜谱", re.I)
_ATOMIC = re.compile(
    r"\b(where|what|which|how|list|find)\b|[?？]|在哪里|是什么|有哪些|怎么查|查询|定位",
    re.I,
)
_CONFIGURE = re.compile(
    r"\b(configure|design|build|implement|set\s*up|roll\s*out)\b|帮我配置|设计|实施|落地|全套方案|新仓",
    re.I,
)


@dataclass(frozen=True, slots=True)
class IntentClassification:
    intent: IntentType
    confidence: float
    reason: str
    retries: int = 0
    tokens_used: int = 0


class IntentClassifier:
    def __init__(
        self,
        llm: BaseLLM,
        *,
        confidence_threshold: float,
        max_retries: int,
        prompt_path: str | Path,
    ) -> None:
        if not 0 < confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be greater than 0 and at most 1")
        self.llm = llm
        self.confidence_threshold = confidence_threshold
        self.max_retries = max_retries
        self.prompt = Path(prompt_path).read_text(encoding="utf-8")

    def classify(self, user_message: str) -> IntentClassification:
        message = user_message.strip()
        if not message:
            raise ValueError("user_message must be non-empty")
        rule_result = self._rule_classify(message)
        if rule_result is not None:
            return rule_result
        invocation = invoke_json(
            self.llm,
            [{"role": "user", "content": self.prompt.replace("{user_message}", message)}],
            max_retries=self.max_retries,
            validator=self._validate_payload,
        )
        try:
            classification = self._from_invocation(invocation)
        except ValueError as exc:
            raise StructuredLLMError(
                str(exc), retries=invocation.retries, tokens_used=invocation.tokens_used
            ) from exc
        return IntentClassification(
            classification.intent,
            classification.confidence,
            classification.reason,
            invocation.retries,
            invocation.tokens_used,
        )

    def requires_clarification(self, result: IntentClassification) -> bool:
        return result.confidence < self.confidence_threshold

    @staticmethod
    def _rule_classify(message: str) -> IntentClassification | None:
        if _INSPECT.search(message):
            return IntentClassification(IntentType.INSPECT_DRAFT, 1.0, "draft inspection rule")
        if _UNSUPPORTED.search(message):
            return IntentClassification(IntentType.UNSUPPORTED, 1.0, "unsupported topic rule")
        question_prefix = re.search(
            r"^\s*(where|what|which|how|list|find|怎么|如何|哪里|什么|哪些)", message, re.I
        )
        if _CONFIGURE.search(message) and question_prefix is None:
            return IntentClassification(IntentType.CONFIGURE_GOAL, 1.0, "configuration goal rule")
        if _ATOMIC.search(message) and not re.search(r"全套|完整方案|end.to.end", message, re.I):
            return IntentClassification(IntentType.ATOMIC_QUERY, 1.0, "one-time question rule")
        if _CONFIGURE.search(message):
            return IntentClassification(IntentType.CONFIGURE_GOAL, 1.0, "configuration goal rule")
        return None

    @staticmethod
    def _from_invocation(invocation: JSONInvocation) -> IntentClassification:
        payload = invocation.payload
        try:
            intent = IntentType(str(payload["intent"]))
            confidence = float(payload["confidence"])
            reason = str(payload["reason"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid intent classification payload") from exc
        if not 0 <= confidence <= 1 or not reason:
            raise ValueError("invalid intent confidence or reason")
        return IntentClassification(intent, confidence, reason)

    @staticmethod
    def _validate_payload(payload: dict[str, object]) -> None:
        try:
            IntentType(str(payload["intent"]))
            confidence = float(payload["confidence"])
            reason = str(payload["reason"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid intent classification payload") from exc
        if not 0 <= confidence <= 1 or not reason:
            raise ValueError("invalid intent confidence or reason")
