"""Real graph boundaries: context, scoped follow-up retrieval, citations and opt-in review."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agents.repositories import SessionRepository
from agents.runtime import open_configured_checkpointer
from agents.services import SessionService
from agents.supervisor import RequirementSessionRunner, Supervisor
from agents.tools import KnowledgeSearchResult
from agents.tools.knowledge_adapter import _evidence_from_citations
from agents.workspace import Workspace
from core.response.citation_generator import CitationGenerator
from core.settings import load_settings
from tests.unit.test_answer_review import Model, repaired_payload, verdict

GAP = {
    "status": "insufficient_evidence",
    "claims": [],
    "gap": "The supplied evidence does not establish the default.",
}
FACT = "SYN_MODE defaults to MANUAL."


def source(text, identifier="1", version="v1"):
    result = SimpleNamespace(
        chunk_id=identifier,
        text=text,
        score=1.0,
        metadata={"source_relative_path": "synthetic.pdf", "version": version},
    )
    return _evidence_from_citations(
        tuple(CitationGenerator(1800).generate([result])), {identifier: text}
    )[0]


class Search:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.calls = []

    def search(self, query, *, filters, top_k):
        self.calls.append((query, dict(filters), top_k))
        batch = next(self.batches)
        if isinstance(batch, Exception):
            raise batch
        return KnowledgeSearchResult(query, filters, tuple(batch), bool(batch), ())


def run(model, search, *, strategy="standard", budget=12000):
    graph = Supervisor(
        llm=model,
        knowledge_adapter=search,
        settings=replace(load_settings().agent, max_tokens_per_turn=budget),
        workspace=Workspace(
            "workspace:test", "Test", ("synthetic",), ("inbound",), ("TEST-A",), ("test",)
        ),
    ).graph
    return graph._answer_question(
        {
            "status": "created",
            "intent": "atomic_query",
            "latest_user_message": "What is SYN_MODE's default?",
            "confirmed_context": {"product_version": "v1"},
            "answer_strategy": strategy,
        }
    )


def test_full_chunk_condition_after_real_1800_character_cutoff_reaches_first_answer():
    item = source("Background information. " * 100 + FACT)
    assert FACT not in item.excerpt and FACT in item.full_excerpt
    model = Model([repaired_payload(text=FACT)])
    search = Search([[item]])
    result = run(model, search)
    assert result["answer_status"] == "answered"
    assert FACT in model.messages[0][0]["content"]
    assert result["answer_evidence"][0]["supporting_quotes"] == [FACT]
    assert result["answer_evidence"][0]["excerpt"] == item.excerpt
    assert model.calls == len(search.calls) == 1
    assert result["evidence_decision_report"] == {}


def test_targeted_retrieval_preserves_original_question_scope_and_old_counterevidence():
    old = [source("Unrelated reporting section.", str(n)) for n in range(5)]
    new = source(FACT, "new")
    model = Model(
        [
            GAP,
            {"query": "SYN_MODE documented default v1"},
            repaired_payload(source_id="6", text=FACT),
        ]
    )
    search = Search([old, [new]])
    result = run(model, search)
    assert result["answer_status"] == "answered"
    assert result["answer_evidence"][0]["citation_index"] == 6
    assert result["answer_evidence"][0]["chunk_id"] == "new"
    assert (
        search.calls[0][1]
        == search.calls[1][1]
        == {
            "version": "v1",
            "collection": "synthetic",
            "module": "inbound",
            "site": "TEST-A",
            "environment": "test",
        }
    )
    assert search.calls[0][0] != search.calls[1][0]
    assert "What is SYN_MODE's default?" in model.messages[-1][0]["content"]
    assert '"id": "1"' in model.messages[-1][0]["content"]
    assert result["tokens_used"] == 60 and result["tool_calls_made"] == 2


@pytest.mark.parametrize("outcome", ["duplicate", "empty", "failed", "changed"])
def test_no_new_evidence_or_failed_search_preserves_initial_gap_without_loop(outcome):
    item = source("Unrelated reporting section.")
    batch = {
        "duplicate": [item],
        "empty": [],
        "failed": RuntimeError("offline"),
        "changed": [replace(item, full_excerpt=FACT)],
    }[outcome]
    model = Model([GAP, {"query": "SYN_MODE default"}])
    search = Search([[item], batch])
    result = run(model, search)
    assert result["answer_status"] == "insufficient_evidence"
    assert "does not establish the default" in result["assistant_reply"]
    assert len(search.calls) == model.calls == 2
    assert result["tokens_used"] == 40


def test_empty_first_search_can_recover():
    model = Model([{"query": "SYN_MODE default"}, repaired_payload(text=FACT)])
    result = run(model, Search([[], [source(FACT)]]))
    assert result["answer_status"] == "answered" and result["tool_calls_made"] == 2


def test_wrong_version_returned_by_second_search_cannot_support_answer():
    model = Model([GAP, {"query": "SYN_MODE default"}, GAP])
    result = run(
        model, Search([[source("Unrelated reporting section.")], [source(FACT, "wrong", "v2")]])
    )
    assert FACT not in model.messages[-1][0]["content"]
    assert result["answer_status"] == "insufficient_evidence"
    assert result["answer_evidence"] == []


def test_failed_revision_keeps_original_answer_and_accounts_all_calls():
    model = Model([GAP, {"query": "SYN_MODE default"}, {}, {}])
    result = run(model, Search([[source("Unrelated reporting section.")], [source(FACT, "new")]]))
    assert result["answer_status"] == "insufficient_evidence"
    assert result["answer_recovery"] == "generation_failed"
    assert result["tokens_used"] == 80 and result["retry_count"] == 1


@pytest.mark.parametrize("strategy", ["standard", "review"])
def test_budget_stops_before_followup_calls(strategy):
    model = Model([GAP])
    search = Search([[source("Unrelated reporting section.")]])
    result = run(model, search, strategy=strategy, budget=10)
    assert result["pause_reason"] == "token_budget_exceeded"
    assert result["tokens_used"] == 20
    assert result["answer_evidence"] == []
    assert model.calls == len(search.calls) == 1


def test_review_is_executed_only_when_selected_and_returns_verified_citations():
    item = source(FACT)
    decision = verdict()
    decision["decisions"][0]["evidence_ids"] = [item.evidence_id]
    model = Model([repaired_payload(text=FACT), decision])
    result = run(model, Search([[item]]), strategy="review")
    assert result["answer_status"] == "answered" and model.calls == 2
    assert result["evidence_decision_report"]["outcome"] == "accepted"
    assert result["answer_evidence"][0]["supporting_quotes"] == [FACT]
    assert result["tokens_used"] == 40


def test_invalid_rewrite_is_not_used_as_answer_or_retrieval_filter():
    model = Model([GAP, {"query": "lookup", "version": "v2"}])
    search = Search([[source("Unrelated reporting section.")]])
    result = run(model, search)
    assert result["answer_status"] == "insufficient_evidence"
    assert len(search.calls) == 1 and result["tokens_used"] == 40


def test_new_search_scores_do_not_mark_unchanged_evidence_as_mutated():
    old = source("Unrelated reporting section.")
    model = Model([GAP, {"query": "SYN_MODE default"}, repaired_payload(source_id="2", text=FACT)])
    result = run(model, Search([[old], [replace(old, score=0.2), source(FACT, "new")]]))
    assert result["answer_status"] == "answered"
    assert result["answer_recovery"] == "new_evidence"


def test_review_selection_does_not_persist_into_next_checkpoint_turn(tmp_path):
    item = source(FACT)
    decision = verdict()
    decision["decisions"][0]["evidence_ids"] = [item.evidence_id]
    model = Model([repaired_payload(text=FACT), decision, repaired_payload(text=FACT)])
    settings = replace(
        load_settings().agent,
        session_db_path=tmp_path / "sessions.db",
        checkpoint_path=tmp_path / "checkpoint.db",
    )
    repository = SessionRepository(settings.session_db_path)
    runner = RequirementSessionRunner(
        supervisor=Supervisor(
            llm=model, settings=settings, knowledge_adapter=Search([[item], [item]])
        ),
        sessions=SessionService(repository),
    )

    async def scenario():
        async with open_configured_checkpointer(settings) as saver:
            first = await runner.start(
                "What is SYN_MODE's default?", checkpointer=saver, answer_strategy="review"
            )
            second = await runner.continue_session(
                first.session.session_id, "What is SYN_MODE's default?", checkpointer=saver
            )
        return first, second

    first, second = asyncio.run(scenario())
    assert first.state["evidence_decision_report"]["outcome"] == "accepted"
    assert second.state["answer_strategy"] == "standard"
    assert second.state["evidence_decision_report"] == {}
    assert model.calls == 3
    replies = [t for t in repository.list_turns(first.session.session_id) if t.role == "assistant"]
    assert [t.metadata["answer_strategy"] for t in replies] == ["review", "standard"]
