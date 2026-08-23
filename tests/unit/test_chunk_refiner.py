from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.settings import TransformSettings
from core.trace import TraceContext
from core.types import Chunk
from ingestion.transform import BaseTransform, ChunkRefiner


def _chunk(text: str = "MOCA   configuration") -> Chunk:
    return Chunk(
        id="chunk-1",
        text=text,
        metadata={"source_path": "manual.pdf"},
        start_offset=0,
        end_offset=len(text),
        source_ref="doc-1",
    )


def _settings(*, enabled: bool = True, use_llm: bool = False) -> TransformSettings:
    return TransformSettings(enabled=enabled, use_llm=use_llm)


def test_base_transform_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseTransform()


@pytest.mark.parametrize(
    "case",
    json.loads(Path("tests/fixtures/noisy_chunks.json").read_text(encoding="utf-8")),
    ids=lambda case: case["name"],
)
def test_rule_refinement_handles_noise_fixture(case: dict[str, str]) -> None:
    output = ChunkRefiner._rule_based_refine(case["input"])

    assert case["contains"] in output


def test_rule_refinement_preserves_code_block_spacing() -> None:
    text = "Before   text\n\n```moca\npublish data\n  where x = 1\n```\n\nAfter   text"

    output = ChunkRefiner(_settings()).transform([_chunk(text)])[0]

    assert "Before text" in output.text
    assert "publish data\n  where x = 1" in output.text
    assert "After text" in output.text
    assert output.metadata["refined_by"] == "rule"


def test_transform_does_not_mutate_input_and_is_idempotent() -> None:
    original = _chunk("Page 1 of 2\nMOCA    setup")
    refiner = ChunkRefiner(_settings())

    first = refiner.transform([original])[0]
    second = refiner.transform([first])[0]

    assert original.text.startswith("Page")
    assert first.text == second.text == "MOCA setup"
    assert first.id == original.id
    assert first.start_offset == original.start_offset


def test_disabled_refiner_returns_an_independent_copy() -> None:
    original = _chunk()

    output = ChunkRefiner(_settings(enabled=False)).transform([original])[0]

    assert output == original
    assert output is not original
    assert output.metadata is not original.metadata


def test_llm_success_marks_metadata_and_formats_prompt() -> None:
    class FakeLLM:
        prompt = ""

        def generate(self, prompt: str) -> str:
            self.prompt = prompt
            return "Refined MOCA configuration"

    llm = FakeLLM()
    output = ChunkRefiner(_settings(use_llm=True), llm=llm).transform([_chunk()])[0]

    assert output.text == "Refined MOCA configuration"
    assert output.metadata["refined_by"] == "llm"
    assert "MOCA configuration" in llm.prompt


def test_llm_failure_falls_back_without_raising() -> None:
    class BrokenLLM:
        @staticmethod
        def generate(prompt: str) -> str:
            raise ConnectionError("offline")

    output = ChunkRefiner(_settings(use_llm=True), llm=BrokenLLM()).transform(
        [_chunk()]
    )[0]

    assert output.text == "MOCA configuration"
    assert output.metadata["refined_by"] == "rule"
    assert output.metadata["refinement_fallback_reason"] == "ConnectionError"


def test_missing_llm_falls_back_and_trace_records_counts() -> None:
    trace = TraceContext("ingestion")

    output = ChunkRefiner(_settings(use_llm=True)).transform([_chunk()], trace)[0]

    assert output.metadata["refinement_fallback_reason"] == "llm_unavailable"
    stage = trace.to_dict()["stages"][0]
    assert stage["name"] == "transform.chunk_refiner"
    assert stage["details"]["fallback_count"] == 1


def test_prompt_without_placeholder_gets_safe_fallback_placeholder(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Clean this fragment", encoding="utf-8")

    refiner = ChunkRefiner(_settings(), prompt_path=prompt)

    assert "{text}" in refiner.prompt
