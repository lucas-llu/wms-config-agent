from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.settings import TransformSettings
from core.trace import TraceContext
from core.types import Chunk
from ingestion.transform import BaseTransform, ChunkRefiner
from libs.llm import ChatResponse


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

        def chat(self, messages, trace=None) -> ChatResponse:
            self.prompt = messages[0]["content"]
            return ChatResponse("Refined MOCA configuration")

    llm = FakeLLM()
    output = ChunkRefiner(_settings(use_llm=True), llm=llm).transform([_chunk()])[0]

    assert output.text == "Refined MOCA configuration"
    assert output.metadata["refined_by"] == "llm"
    assert "MOCA configuration" in llm.prompt


def test_llm_failure_falls_back_without_raising() -> None:
    class BrokenLLM:
        @staticmethod
        def chat(messages, trace=None) -> ChatResponse:
            raise ConnectionError("offline")

    output = ChunkRefiner(_settings(use_llm=True), llm=BrokenLLM()).transform([_chunk()])[0]

    assert output.text == "MOCA configuration"
    assert output.metadata["refined_by"] == "rule"
    assert output.metadata["refinement_fallback_reason"] == "llm_failed_or_empty"


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


def test_nested_markdown_list_indentation_is_preserved() -> None:
    text = "- parent\n  - child\n    - grandchild"

    assert ChunkRefiner._rule_based_refine(text) == text


def test_indented_markdown_code_block_is_preserved() -> None:
    text = "    select *\n      from policy"

    assert ChunkRefiner._rule_based_refine(text) == text


def test_markdown_hard_line_break_is_preserved() -> None:
    text = "First line  \nSecond line"

    assert ChunkRefiner._rule_based_refine(text) == text


def test_noise_only_chunk_falls_back_to_original_evidence() -> None:
    original = _chunk("Page 1 of 1\n====")

    output = ChunkRefiner(_settings()).transform([original])[0]

    assert output.text == original.text
    assert output.metadata["refined_by"] == "original"
    assert output.metadata["refinement_fallback_reason"] == "empty_rule_result"


def test_blank_chunk_remains_blank_without_error() -> None:
    output = ChunkRefiner(_settings()).transform([_chunk("   ")])[0]

    assert output.text == ""
    assert output.metadata["refined_by"] == "rule"


def test_unmatched_fenced_code_keeps_internal_spacing() -> None:
    text = "```moca\npublish data\n  where x = 1"

    assert ChunkRefiner._rule_based_refine(text) == text


def test_empty_llm_response_falls_back_to_rule_result() -> None:
    class EmptyLLM:
        @staticmethod
        def chat(messages, trace=None) -> ChatResponse:
            return ChatResponse("   ")

    output = ChunkRefiner(_settings(use_llm=True), llm=EmptyLLM()).transform([_chunk()])[0]

    assert output.text == "MOCA configuration"
    assert output.metadata["refinement_fallback_reason"] == "llm_failed_or_empty"


def test_rule_mode_never_calls_injected_llm() -> None:
    class ExplodingLLM:
        @staticmethod
        def chat(messages, trace=None) -> ChatResponse:
            raise AssertionError("LLM should not be called")

    output = ChunkRefiner(_settings(), llm=ExplodingLLM()).transform([_chunk()])[0]

    assert output.metadata["refined_by"] == "rule"


def test_llm_refine_contract_returns_string_or_none() -> None:
    class FakeLLM:
        @staticmethod
        def chat(messages, trace=None) -> ChatResponse:
            return ChatResponse("refined")

    assert ChunkRefiner(_settings(), llm=FakeLLM())._llm_refine("text") == "refined"
    assert ChunkRefiner(_settings())._llm_refine("text") is None


def test_one_chunk_exception_does_not_block_the_batch(monkeypatch) -> None:
    original_refine = ChunkRefiner._rule_based_refine

    def selective_refine(text: str) -> str:
        if text == "bad chunk":
            raise ValueError("malformed")
        return original_refine(text)

    monkeypatch.setattr(ChunkRefiner, "_rule_based_refine", staticmethod(selective_refine))
    outputs = ChunkRefiner(_settings()).transform([_chunk("bad chunk"), _chunk("MOCA   setup")])

    assert outputs[0].text == "bad chunk"
    assert outputs[0].metadata["refined_by"] == "original"
    assert outputs[1].text == "MOCA setup"


def test_missing_prompt_file_uses_default_template(tmp_path: Path) -> None:
    refiner = ChunkRefiner(_settings(), prompt_path=tmp_path / "missing.txt")

    assert "{text}" in refiner.prompt
