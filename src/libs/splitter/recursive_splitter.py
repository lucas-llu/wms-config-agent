"""Deterministic Markdown-aware recursive text splitter."""

from __future__ import annotations

import re

from libs.splitter.base_splitter import BaseSplitter

_FENCED_CODE = re.compile(r"(?:^|\n)(```|~~~).*?(?:\n\1)(?=\n|$)", re.DOTALL)
_BREAKS = ("\n\n", "\n#", "\n", "。", ". ", "；", "; ", "，", ", ", " ")


class RecursiveSplitter(BaseSplitter):
    """Use preferred textual boundaries while keeping fenced code blocks intact."""

    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 150) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than 0")
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap must be greater than or equal to 0")
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split_text(self, text: str, trace: object | None = None) -> list[str]:
        del trace
        if not text or not text.strip():
            return []

        chunks: list[str] = []
        cursor = 0
        for match in _FENCED_CODE.finditer(text):
            chunks.extend(self._split_plain(text[cursor : match.start()]))
            code_block = match.group(0).strip()
            if code_block:
                chunks.append(code_block)
            cursor = match.end()
        chunks.extend(self._split_plain(text[cursor:]))
        return [chunk for chunk in chunks if chunk]

    def _split_plain(self, text: str) -> list[str]:
        if not text.strip():
            return []
        if len(text.strip()) <= self.chunk_size:
            return [text.strip()]

        chunks: list[str] = []
        start = 0
        text_length = len(text)
        while start < text_length:
            max_end = min(start + self.chunk_size, text_length)
            end = max_end
            if max_end < text_length:
                minimum_break = start + max(self.chunk_size // 2, 1)
                end = self._preferred_break(text, minimum_break, max_end)
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= text_length:
                break
            next_start = max(start + 1, end - self.chunk_overlap)
            start = self._skip_leading_whitespace(text, next_start)
        return chunks

    @staticmethod
    def _preferred_break(text: str, minimum: int, maximum: int) -> int:
        window = text[minimum:maximum]
        best = -1
        best_length = 0
        for separator in _BREAKS:
            position = window.rfind(separator)
            if position > best:
                best = position
                best_length = len(separator)
        return maximum if best < 0 else minimum + best + best_length

    @staticmethod
    def _skip_leading_whitespace(text: str, start: int) -> int:
        while start < len(text) and text[start].isspace():
            start += 1
        return start
