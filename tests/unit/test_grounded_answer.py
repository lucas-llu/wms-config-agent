import json

import pytest

from agents import Evidence
from agents.llm_json import StructuredLLMError
from agents.nodes.grounded_answer import answer_question, clean_excerpt
from libs.llm import ChatResponse


def evidence():
    return (
        Evidence(
            evidence_id="e:1",
            chunk_id="c:1",
            source="synthetic.pdf",
            excerpt="For the SLOT handling unit, LPN Tracked is No. "
            "For the TROLLEY handling unit, LPN Tracked is Yes.",
            score=1,
            page_start=6,
        ),
    )


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        assert "untrusted DATA" in messages[0]["content"]
        return ChatResponse(json.dumps(self.payload), metadata={"usage": {"total_tokens": 20}})


def payload():
    return {
        "status": "answered",
        "gap": "确认不是取消扫描确认。",
        "claims": [
            {
                "text": "Slot 使用的 LPN Tracked 为 No，不要混改 trolley。",
                "source_id": "1",
                "quote": "For the SLOT handling unit, LPN Tracked is No.",
            }
        ],
    }


def test_answer_leads_with_conclusion_and_validated_quote():
    result = answer_question(FakeLLM(payload()), "slot不跟踪ID", evidence())
    assert result.text.startswith("结论")
    assert "LPN Tracked 为 No" in result.text
    assert "synthetic.pdf" not in result.text and "引用依据" not in result.text
    assert result.supporting_quotes == ((1, "For the SLOT handling unit, LPN Tracked is No."),)
    assert result.tokens_used == 20


@pytest.mark.parametrize(
    "change",
    [{"source_id": "999"}, {"quote": "Set Track Slot ID to No"}, {"quote": "No"}, {"text": ""}],
)
def test_fabricated_citation_or_empty_claim_rejected(change):
    value = payload()
    value["claims"][0].update(change)
    llm = FakeLLM(value)
    with pytest.raises(StructuredLLMError):
        answer_question(llm, "question", evidence())
    assert llm.calls == 2


def test_evidence_gap_does_not_dump_unrelated_excerpts():
    llm = FakeLLM(
        {
            "status": "insufficient_evidence",
            "claims": [],
            "gap": "缺少扫描确认字段，需要流程配置文档。",
        }
    )
    result = answer_question(llm, "不扫描slot", evidence())
    assert "不足" in result.text
    assert "LPN Tracked is" not in result.text


def test_image_markers_removed_before_generation():
    assert clean_excerpt("before [IMAGE: abc_1_1] after") == "before after"


def test_chinese_question_rejects_english_answer_but_preserves_original_quote():
    english = payload()
    english["gap"] = "Confirm the version."
    english["claims"][0]["text"] = "Set LPN Tracked to No for the slot."
    llm = FakeLLM(english)
    with pytest.raises(StructuredLLMError):
        answer_question(llm, "如何不跟踪 slot ID？", evidence())
    assert llm.calls == 2
    chinese = answer_question(FakeLLM(payload()), "如何不跟踪 slot ID？", evidence())
    assert chinese.supporting_quotes[0][1].startswith("For the SLOT handling unit")


def test_english_answer_and_explicit_language_override_use_matching_labels():
    value = payload()
    value["gap"] = "Confirm the version."
    value["claims"][0]["text"] = "The slot handling unit uses LPN Tracked = No."
    result = answer_question(FakeLLM(value), "请用英文回答如何设置 slot？", evidence())
    assert result.text.startswith("Conclusion")
    assert "Supporting evidence" not in result.text and "Quote:" not in result.text
    assert result.supporting_quotes
    assert "结论" not in result.text and "需要确认" not in result.text
