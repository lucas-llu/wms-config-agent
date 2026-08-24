"""Rule-based chunk metadata enrichment with optional structured LLM output."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from core.settings import Settings, TransformSettings
from core.types import Chunk
from ingestion.transform.base_transform import BaseTransform
from ingestion.transform.llm_output_guard import LLMOutputGuard
from libs.llm import BaseLLM

_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9_-]*(?:\.[A-Z0-9_-]+)+\b")
_TECH_TERMS = re.compile(
    r"\b(?:MOCA|WMS|JDA|Blue Yonder|SQL|RF|API|CSV|XML|JSON|putaway|receiving|shipping)\b",
    re.IGNORECASE,
)
_IMAGE_PLACEHOLDER = re.compile(r"\[IMAGE:\s*[^\]]+\]")
_DEFAULT_PROMPT = """Analyze this WMS/JDA MOCA fragment. Return one JSON object with non-empty
string fields title and summary, plus a tags array. Do not invent configuration values.

Fragment:
{text}
"""


class MetadataEnricher(BaseTransform):
    """Add deterministic title, summary, and retrieval tags to every chunk."""

    name = "metadata_enricher"

    def __init__(
        self,
        settings: Settings | TransformSettings,
        llm: BaseLLM | None = None,
        prompt_path: str | Path | None = None,
    ) -> None:
        config = (
            settings.ingestion.metadata_enricher if isinstance(settings, Settings) else settings
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
                details={"input_count": len(chunks), "enriched_count": 0, "disabled": True},
            )
            return result

        result: list[Chunk] = []
        llm_count = 0
        fallback_count = 0
        for chunk in chunks:
            metadata = dict(chunk.metadata)
            rule_values = self._rule_metadata(chunk.text, metadata)
            values = rule_values
            enriched_by = "rule"
            if self.use_llm:
                llm_values, reason = self._llm_metadata(chunk.text)
                if llm_values is not None:
                    values = self._merge_llm_values(rule_values, llm_values, metadata)
                    enriched_by = "llm"
                    llm_count += 1
                    metadata.pop("metadata_enrichment_fallback_reason", None)
                else:
                    fallback_count += 1
                    metadata["metadata_enrichment_fallback_reason"] = reason

            metadata.update(values)
            metadata["metadata_enriched_by"] = enriched_by
            result.append(self.clone_chunk(chunk, metadata=metadata))

        self.record_trace(
            trace,
            name=self.name,
            started=started,
            details={
                "input_count": len(chunks),
                "enriched_count": len(result),
                "llm_count": llm_count,
                "fallback_count": fallback_count,
            },
        )
        return result

    @staticmethod
    def _rule_metadata(text: str, metadata: dict[str, Any]) -> dict[str, Any]:
        normalized = re.sub(r"\s+", " ", _IMAGE_PLACEHOLDER.sub("", text)).strip()
        title = str(metadata.get("title") or "").strip()
        if not title:
            title = MetadataEnricher._derive_title(text)
        summary = MetadataEnricher._derive_summary(normalized)
        tags = MetadataEnricher._derive_tags(text, metadata)
        return {
            "title": title or "WMS configuration fragment",
            "summary": summary or "WMS configuration content.",
            "tags": tags or ["wms-configuration"],
        }

    @staticmethod
    def _derive_title(text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            heading = re.match(r"^#{1,6}\s+(.+)$", line)
            if heading:
                return heading.group(1).strip()[:160]
        if not lines:
            return "WMS configuration fragment"
        clean = _IMAGE_PLACEHOLDER.sub("", lines[0]).strip(" :-")
        if len(clean) <= 160:
            return clean
        return clean[:157].rstrip() + "..."

    @staticmethod
    def _derive_summary(normalized: str) -> str:
        if len(normalized) <= 320:
            return normalized
        candidate = normalized[:321]
        sentence_end = max(candidate.rfind(". "), candidate.rfind("。"), candidate.rfind("; "))
        if sentence_end >= 120:
            return candidate[: sentence_end + 1].strip()
        return candidate[:317].rstrip() + "..."

    @staticmethod
    def _derive_tags(text: str, metadata: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        existing = metadata.get("tags", [])
        if isinstance(existing, str):
            candidates.append(existing)
        elif isinstance(existing, list):
            candidates.extend(str(value) for value in existing)
        for key in ("process_code", "domain", "module", "process_stage", "document_type"):
            value = metadata.get(key)
            if isinstance(value, str):
                candidates.append(value)
        candidates.extend(_IDENTIFIER.findall(text))
        candidates.extend(match.group(0) for match in _TECH_TERMS.finditer(text))

        tags: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            value = re.sub(r"\s+", " ", candidate).strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                tags.append(value)
            if len(tags) == 12:
                break
        return tags

    def _llm_metadata(self, text: str) -> tuple[dict[str, Any] | None, str | None]:
        if self.llm is None:
            return None, "llm_unavailable"
        try:
            response = self.llm.chat([{"role": "user", "content": self.prompt.format(text=text)}])
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.content.strip())
            payload = json.loads(cleaned)
            if not self._valid_llm_payload(payload):
                return None, "invalid_llm_response"
            guard = LLMOutputGuard.validate_metadata(text, payload)
            if not guard.accepted:
                return None, f"guard_{guard.reason}"
            return payload, None
        except Exception as exc:
            return None, type(exc).__name__

    @staticmethod
    def _valid_llm_payload(payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and isinstance(payload.get("title"), str)
            and bool(payload["title"].strip())
            and isinstance(payload.get("summary"), str)
            and bool(payload["summary"].strip())
            and isinstance(payload.get("tags"), list)
            and any(isinstance(tag, str) and tag.strip() for tag in payload["tags"])
        )

    @staticmethod
    def _merge_llm_values(
        rule_values: dict[str, Any],
        llm_values: dict[str, Any],
        existing_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        title = (
            rule_values["title"] if existing_metadata.get("title") else llm_values["title"].strip()
        )
        combined_tags: list[str] = []
        seen: set[str] = set()
        for candidate in [*rule_values["tags"], *llm_values["tags"]]:
            value = str(candidate).strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                combined_tags.append(value)
            if len(combined_tags) == 12:
                break
        return {
            "title": title,
            "summary": llm_values["summary"].strip(),
            "tags": combined_tags,
        }

    @staticmethod
    def _load_prompt(prompt_path: str | Path | None) -> str:
        if prompt_path is None:
            return _DEFAULT_PROMPT
        try:
            prompt = Path(prompt_path).read_text(encoding="utf-8")
        except OSError:
            return _DEFAULT_PROMPT
        return prompt if "{text}" in prompt else f"{prompt.rstrip()}\n\n{{text}}\n"
