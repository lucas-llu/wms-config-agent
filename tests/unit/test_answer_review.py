from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agents.contracts import Evidence
from agents.nodes.grounded_answer import GroundedAnswer
from agents.services.answer_review import review_answer
from agents.services.evidence_decision import EvidenceClaim, EvidenceDecisionService
from libs.llm import ChatResponse


def sources():
    return (Evidence("e:1", "c:1", "synthetic.pdf", "SYN_MODE records events.", 1.0),)


def baseline():
    return GroundedAnswer(
        "SYN_MODE records events.",
        10,
        0,
        claims=(
            EvidenceClaim(
                "claim:1",
                "SYN_MODE records events.",
                ("e:1",),
                "SYN_MODE records events.",
            ),
        ),
    )


def verdict(relation="supported"):
    return {
        "decisions": [
            {
                "claim_id": "claim:1",
                "relation": relation,
                "scope": "compatible",
                "conditions": "preserved",
                "confidence": 0.95,
                "evidence_ids": ["e:1"],
                "reason_code": "direct_support"
                if relation == "supported"
                else "semantic_overreach",
            }
        ]
    }


class Model:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = 0
        self.messages = []

    def chat(self, messages):
        self.calls += 1
        self.messages.append(messages)
        return ChatResponse(
            json.dumps(next(self.outputs)), metadata={"usage": {"total_tokens": 20}}
        )


def repaired_payload(source_id="1", text="SYN_MODE records events."):
    return {
        "status": "answered",
        "gap": "",
        "claims": [
            {
                "text": text,
                "source_id": source_id,
                "quote": text,
            }
        ],
    }


def test_supported_initial_answer_does_not_trigger_revision():
    generator = Model([])
    result = review_answer(
        generator,
        EvidenceDecisionService(Model([verdict()])),
        "What does SYN_MODE do?",
        sources(),
        baseline=baseline(),
    )
    assert result.outcome == "accepted" and result.final.claims == baseline().claims
    assert result.tokens_used == 30 and generator.calls == 0


def test_rejected_draft_is_revised_then_independently_rechecked():
    original = replace(
        baseline(),
        text="SYN_MODE forces scanning.",
        claims=(
            replace(
                baseline().claims[0],
                text="SYN_MODE forces scanning.",
            ),
        ),
    )
    generator = Model([repaired_payload()])
    judge = Model([verdict("insufficient"), verdict()])
    result = review_answer(
        generator,
        EvidenceDecisionService(judge),
        "What does SYN_MODE do?",
        sources(),
        baseline=original,
    )
    assert result.outcome == "revised" and result.revisions == 1
    assert result.tokens_used == 70 and judge.calls == 2 and generator.calls == 1
    assert "forces scanning" not in result.final.text
    assert "review_feedback" in generator.messages[0][0]["content"]


def test_second_rejection_abstains_without_an_unbounded_repair_loop():
    generator = Model([repaired_payload()])
    judge = Model([verdict("insufficient"), verdict("insufficient")])
    result = review_answer(
        generator,
        EvidenceDecisionService(judge),
        "What does SYN_MODE do?",
        sources(),
        baseline=baseline(),
    )
    assert result.outcome == "abstained" and result.final.status == "insufficient_evidence"
    assert result.final.claims == () and result.revisions == 1 and generator.calls == 1


def test_judge_unavailable_never_falls_back_to_unreviewed_answer():
    generator = Model([])
    judge = Model([{"bad": "output"}])
    result = review_answer(
        generator,
        EvidenceDecisionService(judge),
        "What does SYN_MODE do?",
        sources(),
        baseline=baseline(),
    )
    assert result.outcome == "abstained" and generator.calls == 0
    assert result.tokens_used == 30


def test_correct_insufficient_evidence_status_is_not_upgraded_by_supported_claims():
    original = replace(baseline(), status="insufficient_evidence")
    result = review_answer(
        Model([]),
        EvidenceDecisionService(Model([verdict()])),
        "Is scanning mandatory?",
        sources(),
        baseline=original,
    )
    assert (
        result.final.status == "insufficient_evidence" and result.outcome == "insufficient_evidence"
    )


def test_one_retrieval_can_supply_missing_evidence_and_preserve_accounting():
    old = GroundedAnswer("Need documentation.", 10, 0, status="insufficient_evidence")
    new_source = Evidence("e:1", "c:1", "synthetic.pdf", "SYN_MODE records events.", 1.0)
    calls = []

    def retrieve(question, context, reasons):
        calls.append(question)
        return (new_source,)

    result = review_answer(
        Model([repaired_payload()]),
        EvidenceDecisionService(Model([verdict()])),
        "What does SYN_MODE do?",
        (),
        baseline=old,
        retrieve=retrieve,
    )
    assert result.outcome == "revised" and result.retrievals == 1 and len(calls) == 1
    assert result.tokens_used == 50


@pytest.mark.parametrize("kind", ["exception", "bad_shape", "changed_source"])
def test_retrieval_failures_or_mutating_evidence_abstain(kind):
    def retrieve(question, context, reasons):
        if kind == "exception":
            raise RuntimeError("private backend details")
        if kind == "bad_shape":
            return list(sources())
        return (replace(sources()[0], excerpt="Different authoritative document body."),)

    result = review_answer(
        Model([]),
        EvidenceDecisionService(Model([verdict("insufficient")])),
        "What does SYN_MODE do?",
        sources(),
        baseline=baseline(),
        retrieve=retrieve,
    )
    assert result.outcome == "abstained" and result.retrievals == 1
    assert "private" not in result.final.text


def test_counterevidence_pool_is_shared_and_source_scopes_remain_distinguishable():
    pool = sources() + (
        replace(
            sources()[0],
            evidence_id="e:2",
            chunk_id="c:2",
            product_version="v2",
            excerpt="SYN_MODE does not record events.",
        ),
    )
    model = Model([verdict()])
    service = EvidenceDecisionService(model, include_counter_evidence=True)
    service.evaluate("What does it do?", baseline().claims, pool)
    data = json.loads(model.messages[0][1]["content"])
    assert len(data["evidence_pool"]) == 2
    assert data["claims"][0]["evidence"] == [{"evidence_id": "e:1"}]
    assert "Different explicit versions" not in data["evidence_pool"][1]["text"]


def test_verbatim_quote_in_full_context_is_valid_after_revision():
    evidence = (
        replace(sources()[0], full_excerpt="SYN_MODE records events. Restart is required."),
    )
    claim = replace(baseline().claims[0], text="Restart is required.", quote="Restart is required.")
    report = EvidenceDecisionService(Model([verdict()])).evaluate(
        "What is needed?", (claim,), evidence
    )
    assert report.status == "completed"


def test_verified_claims_do_not_allow_unreviewed_gap_advice_through():
    original = replace(baseline(), text="SYN_MODE records events. Set UNDOCUMENTED_FLAG=9.")
    result = review_answer(
        Model([]),
        EvidenceDecisionService(Model([verdict()])),
        "What does it do?",
        sources(),
        baseline=original,
    )
    assert result.outcome == "accepted"
    assert "SYN_MODE records events" in result.final.text
    assert "UNDOCUMENTED_FLAG" not in result.final.text


def test_quote_mismatch_can_expand_to_full_context_then_reverify_a_new_answer():
    evidence = (replace(sources()[0], full_excerpt="Restart is required."),)
    generation = Model([repaired_payload(text="Restart is required.")])
    judge = Model([verdict()])
    result = review_answer(
        generation,
        EvidenceDecisionService(judge),
        "What is required?",
        evidence,
        baseline=baseline(),
    )
    assert result.outcome == "revised" and result.revisions == 1
    assert result.reports[0].error_code == "quote_not_in_evidence"
    assert result.reports[1].status == "completed" and judge.calls == 1
    assert "Restart is required" in result.final.text


def test_fabricated_quote_cannot_use_full_context_recovery():
    original = replace(
        baseline(), claims=(replace(baseline().claims[0], quote="Fabricated quote."),)
    )
    evidence = (replace(sources()[0], full_excerpt="Restart is required."),)
    generation = Model([])
    result = review_answer(
        generation,
        EvidenceDecisionService(Model([])),
        "What is required?",
        evidence,
        baseline=original,
    )
    assert result.outcome == "abstained" and generation.calls == 0


def test_short_quote_cannot_use_full_context_recovery():
    original = replace(baseline(), claims=(replace(baseline().claims[0], quote="events"),))
    evidence = (replace(sources()[0], full_excerpt="Restart is required."),)
    generation = Model([])
    result = review_answer(
        generation,
        EvidenceDecisionService(Model([])),
        "What is required?",
        evidence,
        baseline=original,
    )
    assert result.outcome == "abstained" and generation.calls == 0


def test_failed_revision_is_bounded_and_its_tokens_are_not_lost():
    generator = Model([{}, {}])
    result = review_answer(
        generator,
        EvidenceDecisionService(Model([verdict("insufficient")])),
        "What does it do?",
        sources(),
        baseline=baseline(),
    )
    assert result.outcome == "abstained" and result.error_code == "revision_unavailable"
    assert result.tokens_used == 70 and generator.calls == 2 and result.revisions == 1


def test_cached_baseline_citations_are_checked_against_the_original_sources():
    from scripts.evaluate_answer_review import restore_baseline

    case = {"id": "cache", "sources": [{"id": "one", "text": "SYN_MODE records events."}]}
    record = {
        "text": "SYN_MODE records events.",
        "tokens_used": 10,
        "status": "answered",
        "claims": [
            {
                "text": "SYN_MODE records events.",
                "evidence_ids": ["e:cache:one"],
                "quote": "SYN_MODE records events.",
            }
        ],
    }
    restored = restore_baseline(case, record)
    assert restored.claims[0].text == record["claims"][0]["text"]
    assert restored.cited_source_ids == (1,)
    record["claims"][0]["evidence_ids"] = ["e:wrong"]
    with pytest.raises(ValueError):
        restore_baseline(case, record)


@pytest.mark.parametrize("count", [-1, 2, True, 1.0, "1"])
def test_revision_budget_cannot_be_unbounded(count):
    with pytest.raises(ValueError):
        review_answer(
            Model([]),
            EvidenceDecisionService(Model([])),
            "q",
            (),
            baseline=baseline(),
            max_revisions=count,
        )
