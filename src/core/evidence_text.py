"""Clean extracted evidence for presentation without changing stored source text."""

from __future__ import annotations

import re

_IMAGE = re.compile(r"\[IMAGE\s*:\s*([^\]\r\n]*)\]", re.I)
_HASH_IMAGE = re.compile(r"([a-f0-9]{64})_\d+_\d+", re.I)


def image_references(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            m.group(1).strip()
            for m in _IMAGE.finditer(text)
            if re.fullmatch(r"[A-Za-z0-9_.:-]+", m.group(1).strip())
        )
    )


def image_document_hash(text: str) -> str | None:
    hashes = {
        m.group(1).lower()
        for value in image_references(text)
        if (m := _HASH_IMAGE.fullmatch(value))
    }
    return next(iter(hashes)) if len(hashes) == 1 else None


def clean_evidence_text(text: str) -> str:
    text = _IMAGE.sub("", text)
    text = re.sub(r"\[IMAGE\s*:[^\]\r\n]*(?=$|\r?\n)", "", text, flags=re.I)
    text = re.sub(r"(?<!\w)[a-f0-9]{8,64}_\d+_\d+\]", "", text, flags=re.I)
    text = re.sub(r"(?<!\w)[a-f0-9]{32,64}_\d+_\d+", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()
