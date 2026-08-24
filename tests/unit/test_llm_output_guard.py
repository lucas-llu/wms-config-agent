from __future__ import annotations

import json

import pytest

from core.settings import TransformSettings
from core.types import Chunk
from ingestion.transform import ChunkRefiner, LLMOutputGuard, MetadataEnricher
from libs.llm import ChatResponse


def _chunk(text: str) -> Chunk:
    return Chunk(
        id="guard-test",
        text=text,
        metadata={"source_path": "manual.pdf"},
        start_offset=0,
        end_offset=len(text),
        source_ref="document-1",
    )


def test_refinement_guard_accepts_cleanup_that_preserves_technical_evidence() -> None:
    source = "Run MOCA command SWL.I.11.01 with policy.max_weight 10.5."
    candidate = "Run MOCA command SWL.I.11.01 using policy.max_weight 10.5."

    assert LLMOutputGuard.validate_refinement(source, candidate).accepted is True


@pytest.mark.parametrize(
    "candidate, reason, token",
    [
        (
            "Configure the receiving policy.",
            "missing_technical_tokens",
            "SWL.I.11.01",
        ),
        (
            "Configure SWL.I.11.01 and SWL.I.99.99.",
            "invented_technical_tokens",
            "SWL.I.99.99",
        ),
    ],
)
def test_refinement_guard_rejects_lost_or_invented_identifiers(
    candidate: str, reason: str, token: str
) -> None:
    result = LLMOutputGuard.validate_refinement(
        "Configure SWL.I.11.01 receiving policy.", candidate
    )

    assert result.accepted is False
    assert result.reason == reason
    assert token in (*result.missing_tokens, *result.added_tokens)


def test_refinement_guard_rejects_modified_fenced_code() -> None:
    source = "Before\n```moca\npublish data\n where x = 1\n```\nAfter"
    candidate = "Before\n```moca\npublish data\n where x = 2\n```\nAfter"

    assert LLMOutputGuard.validate_refinement(source, candidate).reason in {
        "missing_technical_tokens",
        "invented_technical_tokens",
        "modified_code_block",
    }


def test_chunk_refiner_falls_back_when_guard_rejects_llm_output() -> None:
    class UnsafeLLM:
        @staticmethod
        def chat(messages, trace=None) -> ChatResponse:
            return ChatResponse("Configure SWL.I.99.99 receiving policy.")

    source = "Configure SWL.I.11.01 receiving policy."
    output = ChunkRefiner(TransformSettings(enabled=True, use_llm=True), llm=UnsafeLLM()).transform(
        [_chunk(source)]
    )[0]

    assert output.text == source
    assert output.metadata["refined_by"] == "rule"
    assert output.metadata["refinement_fallback_reason"].startswith("guard_")
    assert output.metadata["refinement_guard"]["accepted"] is False


def test_metadata_enricher_rejects_invented_process_code() -> None:
    class UnsafeLLM:
        @staticmethod
        def chat(messages, trace=None) -> ChatResponse:
            return ChatResponse(
                json.dumps(
                    {
                        "title": "Receiving configuration",
                        "summary": "Configure SWL.I.99.99.",
                        "tags": ["SWL.I.99.99"],
                    }
                )
            )

    output = MetadataEnricher(
        TransformSettings(enabled=True, use_llm=True), llm=UnsafeLLM()
    ).transform([_chunk("Configure SWL.I.11.01 receiving policy.")])[0]

    assert output.metadata["metadata_enriched_by"] == "rule"
    assert output.metadata["metadata_enrichment_fallback_reason"] == (
        "guard_invented_technical_tokens"
    )
    assert "SWL.I.99.99" not in output.metadata["tags"]
