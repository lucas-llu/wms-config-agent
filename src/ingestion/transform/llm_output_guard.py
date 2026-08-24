"""Deterministic safety checks for ingestion-time LLM output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_TECHNICAL_TOKEN = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"
    r"|(?<!\w)--?[A-Za-z][A-Za-z0-9_-]*"
    r"|(?<!\w)[@:$][A-Za-z_][A-Za-z0-9_]*"
    r"|\b[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+\b"
    r"|\b[A-Z][A-Z0-9_-]{1,}\b"
    r"|\b\d+(?:\.\d+)+\b"
)
_FENCED_CODE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_GENERIC_TECHNICAL_TERMS = {
    "API",
    "CSV",
    "JDA",
    "JSON",
    "MOCA",
    "RF",
    "SQL",
    "WMS",
    "XML",
}


@dataclass(frozen=True, slots=True)
class GuardResult:
    accepted: bool
    reason: str | None = None
    missing_tokens: tuple[str, ...] = ()
    added_tokens: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"accepted": self.accepted}
        if self.reason:
            payload["reason"] = self.reason
        if self.missing_tokens:
            payload["missing_tokens"] = list(self.missing_tokens)
        if self.added_tokens:
            payload["added_tokens"] = list(self.added_tokens)
        return payload


class LLMOutputGuard:
    """Reject rewrites that lose or invent strong technical evidence."""

    @classmethod
    def validate_refinement(cls, source: str, candidate: str) -> GuardResult:
        if not candidate.strip():
            return GuardResult(False, "empty_output")
        source_tokens = cls.technical_tokens(source)
        candidate_tokens = cls.technical_tokens(candidate)
        missing = tuple(sorted(source_tokens - candidate_tokens))
        added = tuple(sorted(candidate_tokens - source_tokens - _GENERIC_TECHNICAL_TERMS))
        if missing:
            return GuardResult(False, "missing_technical_tokens", missing_tokens=missing)
        if added:
            return GuardResult(False, "invented_technical_tokens", added_tokens=added)

        compact_source = re.sub(r"\s+", "", source)
        compact_candidate = re.sub(r"\s+", "", candidate)
        if len(compact_source) >= 20:
            ratio = len(compact_candidate) / len(compact_source)
            if ratio < 0.45:
                return GuardResult(False, "excessive_content_loss")
            if ratio > 2.5:
                return GuardResult(False, "excessive_content_growth")

        missing_code = tuple(
            block.strip()
            for block in _FENCED_CODE.findall(source)
            if block.strip() and block.strip() not in candidate
        )
        if missing_code:
            return GuardResult(False, "modified_code_block")
        return GuardResult(True)

    @classmethod
    def validate_metadata(cls, source: str, payload: dict[str, Any]) -> GuardResult:
        candidate = "\n".join(
            [
                str(payload.get("title", "")),
                str(payload.get("summary", "")),
                " ".join(str(tag) for tag in payload.get("tags", [])),
            ]
        )
        source_tokens = cls.technical_tokens(source)
        candidate_tokens = cls.technical_tokens(candidate)
        added = tuple(sorted(candidate_tokens - source_tokens - _GENERIC_TECHNICAL_TERMS))
        if added:
            return GuardResult(False, "invented_technical_tokens", added_tokens=added)
        return GuardResult(True)

    @staticmethod
    def technical_tokens(text: str) -> set[str]:
        return {match.group(0) for match in _TECHNICAL_TOKEN.finditer(text)}
