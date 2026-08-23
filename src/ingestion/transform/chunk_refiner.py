"""Deterministic chunk cleanup with optional LLM refinement and safe fallback."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from core.settings import Settings, TransformSettings
from core.types import Chunk
from ingestion.transform.base_transform import BaseTransform

_DEFAULT_PROMPT = """Refine this WMS/JDA MOCA fragment without changing technical meaning.
Preserve commands, configuration keys, versions, and identifiers. Return only the refined text.

Fragment:
{text}
"""
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_PAGE_MARKER = re.compile(
    r"^\s*(?:page\s+\d+(?:\s+(?:of|/)\s*\d+)?|"
    r"第\s*\d+\s*页(?:\s*[，,/ ]*共\s*\d+\s*页)?|"
    r"[-–—]+\s*\d+\s*[-–—]+)\s*$",
    re.IGNORECASE,
)
_SEPARATOR = re.compile(r"^\s*[-_=*]{4,}\s*$")
_CODE_FENCE = re.compile(r"^\s*```")


class ChunkRefiner(BaseTransform):
    """Clean noisy prose and optionally delegate a second pass to a text LLM."""

    name = "chunk_refiner"

    def __init__(
        self,
        settings: Settings | TransformSettings,
        llm: Any | None = None,
        prompt_path: str | Path | None = None,
    ) -> None:
        config = (
            settings.ingestion.chunk_refiner
            if isinstance(settings, Settings)
            else settings
        )
        self.enabled = config.enabled
        self.use_llm = config.use_llm
        self.llm = llm
        self.prompt = self._load_prompt(prompt_path or config.prompt_path)

    def transform(self, chunks: list[Chunk], trace: Any | None = None) -> list[Chunk]:
        started = time.perf_counter()
        if not self.enabled:
            result = [self.clone_chunk(chunk) for chunk in chunks]
            self.record_trace(
                trace,
                name=self.name,
                started=started,
                details={"input_count": len(chunks), "refined_count": 0, "disabled": True},
            )
            return result

        result: list[Chunk] = []
        llm_count = 0
        fallback_count = 0
        error_count = 0
        for chunk in chunks:
            metadata = dict(chunk.metadata)
            try:
                refined = self._rule_based_refine(chunk.text)
                refined_by = "rule"
                if self.use_llm:
                    llm_result, fallback_reason = self._llm_refine(refined, trace)
                    if llm_result is not None:
                        refined = llm_result
                        refined_by = "llm"
                        llm_count += 1
                        metadata.pop("refinement_fallback_reason", None)
                    else:
                        fallback_count += 1
                        metadata["refinement_fallback_reason"] = fallback_reason
                metadata["refined_by"] = refined_by
                metadata["refinement_changed"] = refined != chunk.text
                result.append(self.clone_chunk(chunk, text=refined, metadata=metadata))
            except Exception as exc:  # one malformed chunk must not stop a document
                error_count += 1
                metadata["refined_by"] = "original"
                metadata["refinement_fallback_reason"] = type(exc).__name__
                metadata["refinement_changed"] = False
                result.append(self.clone_chunk(chunk, metadata=metadata))

        self.record_trace(
            trace,
            name=self.name,
            started=started,
            details={
                "input_count": len(chunks),
                "refined_count": len(chunks) - error_count,
                "llm_count": llm_count,
                "fallback_count": fallback_count,
                "error_count": error_count,
            },
        )
        return result

    @staticmethod
    def _rule_based_refine(text: str) -> str:
        if not text.strip():
            return text.strip()

        normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
        output: list[str] = []
        prose: list[str] = []
        in_code = False

        def flush_prose() -> None:
            if not prose:
                return
            cleaned = ChunkRefiner._clean_prose("\n".join(prose))
            if cleaned:
                output.extend(cleaned.splitlines())
            prose.clear()

        for line in normalized.splitlines():
            if _CODE_FENCE.match(line):
                flush_prose()
                output.append(line.rstrip())
                in_code = not in_code
            elif in_code:
                output.append(line.rstrip())
            else:
                prose.append(line)
        flush_prose()
        return "\n".join(output).strip()

    @staticmethod
    def _clean_prose(text: str) -> str:
        text = _HTML_COMMENT.sub("", text)
        text = re.sub(r"(?<=[A-Za-z])-[ \t]*\n[ \t]*(?=[a-z])", "", text)
        cleaned_lines: list[str] = []
        for line in text.splitlines():
            if _PAGE_MARKER.match(line) or _SEPARATOR.match(line):
                continue
            cleaned_lines.append(re.sub(r"[ \t]+", " ", line).strip())
        cleaned = "\n".join(cleaned_lines)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    def _llm_refine(
        self, text: str, trace: Any | None = None
    ) -> tuple[str | None, str | None]:
        del trace  # reserved for provider-level tracing
        if self.llm is None:
            return None, "llm_unavailable"
        try:
            response = self.llm.generate(self.prompt.format(text=text))
            value = self._response_text(response)
            if not value:
                return None, "empty_llm_response"
            return value.strip(), None
        except Exception as exc:
            return None, type(exc).__name__

    @staticmethod
    def _response_text(response: Any) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            value = response.get("text") or response.get("content")
            return value if isinstance(value, str) else ""
        value = getattr(response, "content", None)
        return value if isinstance(value, str) else ""

    @staticmethod
    def _load_prompt(prompt_path: str | Path | None) -> str:
        if prompt_path is None:
            return _DEFAULT_PROMPT
        try:
            prompt = Path(prompt_path).read_text(encoding="utf-8")
        except OSError:
            return _DEFAULT_PROMPT
        return prompt if "{text}" in prompt else f"{prompt.rstrip()}\n\n{{text}}\n"
