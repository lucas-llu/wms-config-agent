from __future__ import annotations

import json

from core.settings import TransformSettings
from core.types import Chunk
from ingestion.transform import MetadataEnricher


def _chunk(text: str, metadata: dict | None = None) -> Chunk:
    values = {"source_path": "manual.pdf", **(metadata or {})}
    return Chunk(
        id="chunk-1",
        text=text,
        metadata=values,
        start_offset=0,
        end_offset=len(text),
        source_ref="doc-1",
    )


def _settings(*, use_llm: bool = False) -> TransformSettings:
    return TransformSettings(enabled=True, use_llm=use_llm)


def test_rule_enrichment_always_adds_non_empty_contract_fields() -> None:
    chunk = _chunk(
        "# Receiving configuration\nConfigure MOCA policy SWL.I.01.01 for RF receiving.",
        {"domain": "Inbound", "process_code": "SWL.I.01.01"},
    )

    output = MetadataEnricher(_settings()).transform([chunk])[0]

    assert output.metadata["title"] == "Receiving configuration"
    assert output.metadata["summary"]
    assert "SWL.I.01.01" in output.metadata["tags"]
    assert "Inbound" in output.metadata["tags"]
    assert output.metadata["metadata_enriched_by"] == "rule"


def test_existing_document_title_is_preserved() -> None:
    output = MetadataEnricher(_settings()).transform(
        [_chunk("# Local heading\nDetails", {"title": "Trusted manual title"})]
    )[0]

    assert output.metadata["title"] == "Trusted manual title"


def test_rule_enrichment_is_idempotent_and_does_not_mutate_input() -> None:
    original = _chunk("MOCA configuration for shipping")
    enricher = MetadataEnricher(_settings())

    first = enricher.transform([original])[0]
    second = enricher.transform([first])[0]

    assert first.metadata == second.metadata
    assert "summary" not in original.metadata


def test_llm_json_enrichment_uses_summary_and_combines_tags() -> None:
    class FakeLLM:
        @staticmethod
        def generate(prompt: str) -> str:
            assert "MOCA" in prompt
            return json.dumps(
                {
                    "title": "Generated title",
                    "summary": "Configure the receiving policy.",
                    "tags": ["policy", "receiving"],
                }
            )

    output = MetadataEnricher(_settings(use_llm=True), llm=FakeLLM()).transform(
        [_chunk("MOCA receiving configuration", {"domain": "Inbound"})]
    )[0]

    assert output.metadata["title"] == "Generated title"
    assert output.metadata["summary"] == "Configure the receiving policy."
    assert "Inbound" in output.metadata["tags"]
    assert "policy" in output.metadata["tags"]
    assert output.metadata["metadata_enriched_by"] == "llm"


def test_invalid_llm_response_falls_back_to_rule_metadata() -> None:
    class BadLLM:
        @staticmethod
        def generate(prompt: str) -> str:
            return "not json"

    output = MetadataEnricher(_settings(use_llm=True), llm=BadLLM()).transform(
        [_chunk("WMS putaway setup")]
    )[0]

    assert output.metadata["summary"]
    assert output.metadata["metadata_enriched_by"] == "rule"
    assert output.metadata["metadata_enrichment_fallback_reason"] == "JSONDecodeError"
