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
    assert "synthetic.pdf" in result.text and "页码：6" in result.text
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
