"""Split old inline bibliography blocks for display without rewriting saved turns."""

from __future__ import annotations

import re

from core.evidence_text import clean_evidence_text

_HEADING = re.compile(r"(?m)^\s*(?:#{1,6}\s*)?(?:引用依据|Supporting evidence)\s*[:：]?\s*$")
_NOTE = re.compile(
    r"(?m)^(?:以上基于文档，尚未核验你的实际环境。|"
    r"Based on documents; your actual environment is not verified\.)\s*$"
)


def split_answer_evidence(message: str) -> tuple[str, str]:
    heading = _HEADING.search(message)
    if heading is None:
        return message, ""
    body, references = message[: heading.start()].rstrip(), message[heading.end() :].strip()
    note = _NOTE.search(references)
    if note:
        body += "\n\n" + note.group().strip()
        references = references[: note.start()].rstrip()
    references = "\n".join(clean_evidence_text(line) for line in references.splitlines())
    return body, references
