from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agents.contracts import Evidence
from agents.nodes.grounded_answer import answer_question
from agents.services.evidence_decision import EvidenceDecisionService
from agents.supervisor import Supervisor
from agents.tools import KnowledgeSearchResult
from core.settings import load_settings
from libs.llm import ChatResponse
from scripts.evaluate_evidence_decisions import NoNetworkLLM, evaluate_cases


class AnswerLLM:
    def chat(self, messages):
        return ChatResponse(
            json.dumps(
                {
                    "status": "answered",
                    "gap": "",
                    "claims": [
                        {
                            "text": "FEATURE_A forces scanning.",
                            "source_id": "1",
                            "quote": "FEATURE_A records scan events.",
                        }
                    ],
                }
            ),
            metadata={"usage": {"total_tokens": 20}},
        )


class DecisionLLM:
    def __init__(self, broken=False):
        self.broken = broken
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return ChatResponse(
            "bad JSON"
            if self.broken
            else json.dumps(
                {
                    "decisions": [
                        {
                            "claim_id": "claim:1",
                            "relation": "insufficient",
                            "scope": "compatible",
                            "conditions": "unknown",
                            "confidence": 0.99,
                            "evidence_ids": ["e:1"],
                            "reason_code": "semantic_overreach",
                        }
                    ]
                }
            ),
            metadata={"usage": {"total_tokens": 37}},
        )


def sources():
    return (Evidence("e:1", "c:1", "synthetic.pdf", "FEATURE_A records scan events.", 1.0),)


@pytest.mark.parametrize("broken", [False, True])
def test_shadow_does_not_change_answer_citations_and_accounts_extra_tokens(broken):
    baseline = answer_question(AnswerLLM(), "What does FEATURE_A do?", sources())
    judge = DecisionLLM(broken)
    result = answer_question(
        AnswerLLM(),
        "What does FEATURE_A do?",
        sources(),
        decision_service=EvidenceDecisionService(judge),
    )
    assert result.text == baseline.text
    assert result.cited_source_ids == baseline.cited_source_ids
    assert result.supporting_quotes == baseline.supporting_quotes
    assert baseline.decision_report is None and baseline.tokens_used == 20
    assert result.tokens_used == 57 and judge.calls == 1
    assert result.decision_report.decisions[0].suggested_action != "candidate_supported"


def test_supervisor_exposes_shadow_report_and_accounts_it_in_turn_budget():
    class Search:
        def search(self, query, *, filters, top_k):
            return KnowledgeSearchResult(query, filters, sources(), True, ())

    judge = DecisionLLM()
    supervisor = Supervisor(
        llm=AnswerLLM(),
        settings=load_settings().agent,
        knowledge_adapter=Search(),
        evidence_decision_service=EvidenceDecisionService(judge),
    )
    state = {"status": "created", "intent": "atomic_query", "latest_user_message": "What is A?"}
    update = supervisor.graph._answer_question(state)
    assert update["answer_status"] == "answered"
    assert update["evidence_decision_report"]["decisions"][0]["suggested_action"] == "retrieve_more"
    assert update["tokens_used"] == 57
    # Persisted observation is JSON-compatible and carries no raw document body.
    assert "records scan" not in json.dumps(update["evidence_decision_report"])

    supervisor.settings = replace(supervisor.settings, max_tokens_per_turn=30)
    supervisor.graph.budget.settings = supervisor.settings
    update = supervisor.graph._answer_question(state)
    assert update["pause_reason"] == "token_budget_exceeded"


def test_preflight_reports_no_semantic_accuracy_or_promotion():
    from pathlib import Path

    cases = json.loads(Path("tests/fixtures/evidence_decision_cases.json").read_text("utf-8"))[
        "cases"
    ]
    result = evaluate_cases(EvidenceDecisionService(NoNetworkLLM()), cases, live=False)
    assert result["sample_count"] == 23
    assert result["preflight_guards_passed"]
    assert result["label_accuracy"] is None and result["false_pass_rate"] is None
    assert result["quote_presence_only_baseline"]["false_pass_count"] == 16
    assert not result["production_ready"] and not result["measures_semantic_quality"]


def test_unavailable_live_backend_counts_abstention_as_false_block_not_correct():
    from pathlib import Path

    cases = json.loads(Path("tests/fixtures/evidence_decision_cases.json").read_text("utf-8"))[
        "cases"
    ]
    result = evaluate_cases(EvidenceDecisionService(NoNetworkLLM()), cases, live=True)
    assert result["false_block_rate"] == 1.0
    assert result["completed_count"] == 0 and not result["production_ready"]
    assert result["label_accuracy"] is None and not result["measures_semantic_quality"]
