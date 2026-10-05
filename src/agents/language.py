"""Conversation language policy, independent of retrieved document language."""

from __future__ import annotations

import re

_HAN = re.compile(r"[\u3400-\u9fff]")
_EN_REQUEST = re.compile(
    r"(?:用|使用|以|请)?\s*(?:英文|英语)\s*(?:回答|回复|解释|输出)|"
    r"(?:answer|reply|respond|explain)\s+(?:to me\s+)?in\s+english",
    re.I,
)
_ZH_REQUEST = re.compile(
    r"(?:用|使用|以|请)?\s*(?:中文|汉语)\s*(?:回答|回复|解释|输出)|"
    r"(?:answer|reply|respond|explain)\s+(?:to me\s+)?in\s+(?:chinese|mandarin)",
    re.I,
)


def response_language(message: str, previous: str = "") -> str:
    """Follow the user's prose; short identifiers inherit the conversation language.

    WMS's supported conversation languages are Chinese and English. Explicit language
    requests override detection. Evidence, prompts and model output never select it.
    """
    requests = [
        (match.start(), language)
        for language, pattern in (("en", _EN_REQUEST), ("zh", _ZH_REQUEST))
        for match in pattern.finditer(message)
    ]
    if requests:
        return max(requests)[1]
    if _HAN.search(message):
        return "zh"
    if re.match(
        r"\s*(how|what|why|where|which|can|could|please|configure|build|design|show|explain|help)\b",
        message,
        re.I,
    ):
        return "en"
    words = re.findall(r"\b[a-z]{2,}\b", message)
    if previous in {"zh", "en"} and len(words) < 3:
        return previous
    return "en"


def language_instruction(language: str) -> str:
    name = "Chinese (中文)" if language == "zh" else "English"
    return (
        f"Response language: {name}. Write all user-facing explanations in {name}, "
        "including conclusions, gaps, summaries and task descriptions. Keep JSON keys, "
        "module identifiers, technical field names and exact evidence quotes unchanged. "
        "The language of retrieved documents must not change this response language.\n"
    )


def validate_language(text: str, language: str) -> None:
    """Reject wholly wrong-language prose; preserve technical literals and quotes."""
    prose = re.sub(r"`[^`]*`", "", text).strip()
    if not prose:
        return
    if language == "zh" and not _HAN.search(prose) and re.search(r"[a-zA-Z]{2,}", prose):
        raise ValueError("User-facing explanations must be in Chinese")
    if language == "en" and _HAN.search(prose):
        raise ValueError("User-facing explanations must be in English")


def localized(language: str, chinese: str, english: str) -> str:
    return chinese if language == "zh" else english
