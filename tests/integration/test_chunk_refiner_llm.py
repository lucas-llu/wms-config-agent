"""Opt-in live-provider acceptance for C5/C6.

Run with WMS_LLM_INTEGRATION=1 after registering and configuring a real provider.
"""

from __future__ import annotations

import os

import pytest

from core.settings import TransformSettings, load_settings
from core.types import Chunk
from ingestion.transform import ChunkRefiner, MetadataEnricher
from libs.llm import LLMFactory

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.getenv("WMS_LLM_INTEGRATION") != "1",
    reason="set WMS_LLM_INTEGRATION=1 to run the configured live LLM acceptance",
)
def test_live_llm_refinement_and_metadata_quality() -> None:
    settings = load_settings()
    if settings.llm.provider == "disabled":
        pytest.fail("WMS_LLM_INTEGRATION=1 requires a configured non-disabled provider")
    llm = LLMFactory.create(settings)
    transform_settings = TransformSettings(enabled=True, use_llm=True)
    source = "Page 1 of 1\nMOCA    policy SWL.I.99.01 configuration"
    chunk = Chunk(
        id="live-llm",
        text=source,
        metadata={"source_path": "live-acceptance.pdf"},
        start_offset=0,
        end_offset=len(source),
    )

    refined = ChunkRefiner(transform_settings, llm=llm).transform([chunk])[0]
    enriched = MetadataEnricher(transform_settings, llm=llm).transform([refined])[0]

    assert refined.metadata["refined_by"] == "llm"
    assert "SWL.I.99.01" in refined.text
    assert "Page 1 of 1" not in refined.text
    assert enriched.metadata["metadata_enriched_by"] == "llm"
    assert enriched.metadata["title"]
    assert enriched.metadata["summary"]
    assert enriched.metadata["tags"]
