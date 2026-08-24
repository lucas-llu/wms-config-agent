import json
from pathlib import Path

from core.types import Chunk
from ingestion.llm_failure_ledger import (
    LLMFailureLedger,
    LLMFallback,
    collect_llm_fallbacks,
)


def _chunk(chunk_id: str, **metadata: object) -> Chunk:
    return Chunk(
        id=chunk_id,
        text="MOCA configuration",
        metadata={"source_path": "manual.pdf", **metadata},
        start_offset=0,
        end_offset=18,
    )


def test_collect_fallbacks_excludes_expected_rule_only_states() -> None:
    chunks = [
        _chunk(
            "chunk-1",
            refinement_fallback_reason="empty_rule_result",
            image_caption_status="disabled",
        ),
        _chunk("chunk-rule-failure", refinement_fallback_reason="ValueError"),
        _chunk(
            "chunk-2",
            refinement_llm_enabled=True,
            refinement_fallback_reason="guard_missing_technical_tokens",
            metadata_enrichment_fallback_reason="LLMBudgetExceeded",
            image_caption_status="partial",
        ),
    ]

    failures = collect_llm_fallbacks(
        chunks,
        document_id="doc-1",
        source_path="manual.pdf",
    )

    assert [(item.chunk_id, item.transform) for item in failures] == [
        ("chunk-2", "chunk_refiner"),
        ("chunk-2", "metadata_enricher"),
        ("chunk-2", "image_captioner"),
    ]


def test_ledger_replaces_one_documents_entries_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "llm_failures.jsonl"
    ledger = LLMFailureLedger(path)
    ledger.update_document(
        "doc-1",
        (
            LLMFallback("doc-1", "b.pdf", "chunk-2", "metadata_enricher", "timeout"),
            LLMFallback("doc-1", "b.pdf", "chunk-1", "chunk_refiner", "empty"),
        ),
    )
    ledger.update_document(
        "doc-2",
        (LLMFallback("doc-2", "a.pdf", "chunk-3", "chunk_refiner", "timeout"),),
    )
    ledger.update_document("doc-1", ())
    ledger.write()

    restored = LLMFailureLedger(path)

    assert restored.count == 1
    assert restored.entries[0].document_id == "doc-2"
    assert json.loads(path.read_text(encoding="utf-8"))["chunk_id"] == "chunk-3"
