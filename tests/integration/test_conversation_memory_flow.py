import asyncio
import json
from dataclasses import replace

import pytest

from agents import Evidence
from agents.repositories import SessionRepository
from agents.runtime import open_configured_checkpointer
from agents.services import SessionService
from agents.supervisor import RequirementSessionRunner, Supervisor
from agents.tools import KnowledgeSearchResult
from core.settings import load_settings
from libs.llm import ChatResponse


class MemoryLLM:
    def __init__(self, *, fail_summary=False, ambiguous=False):
        self.prompts = []
        self.fail_summary = fail_summary
        self.ambiguous = ambiguous

    def chat(self, messages):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if prompt.startswith("TASK: summarize_conversation"):
            value = {"summary": "User wants trolley slot picking without tracking slot ID."}
            if self.fail_summary:
                value = {"summary": ""}
        elif prompt.startswith("TASK: resolve_followup"):
            value = (
                {"question": "", "clarification": "Which option do you mean?"}
                if self.ambiguous
                else {
                    "question": (
                        "Where is the slot option for trolley picking without tracking slot ID?"
                    ),
                    "clarification": "",
                }
            )
        else:
            value = {
                "status": "answered",
                "gap": "",
                "claims": [
                    {
                        "text": "Check the slot handling unit.",
                        "source_id": "1",
                        "quote": "Check the slot handling unit.",
                    }
                ],
            }
        return ChatResponse(json.dumps(value), metadata={"usage": {"total_tokens": 20}})


class Search:
    def __init__(self):
        self.queries = []

    def search(self, query, *, filters, top_k=5):
        self.queries.append(query)
        return KnowledgeSearchResult(
            query,
            filters,
            (
                Evidence(
                    evidence_id="e:slot",
                    chunk_id="c:slot",
                    source="slot.pdf",
                    score=1,
                    excerpt="Check the slot handling unit.",
                ),
            ),
            True,
            (),
        )


def setup(tmp_path, **changes):
    settings = replace(
        load_settings().agent,
        checkpoint_path=tmp_path / "graph.db",
        session_db_path=tmp_path / "sessions.db",
        **changes,
    )
    repository = SessionRepository(settings.session_db_path)
    search = Search()

    async def turn(llm, text, session_id=None):
        runner = RequirementSessionRunner(
            supervisor=Supervisor(llm=llm, settings=settings, knowledge_adapter=search),
            sessions=SessionService(repository),
        )
        async with open_configured_checkpointer(settings) as saver:
            if session_id:
                return await runner.continue_session(session_id, text, checkpointer=saver)
            return await runner.start(text, checkpointer=saver)

    return repository, search, turn


def test_followup_uses_both_roles_and_rolling_summary_survives_restart(tmp_path):
    repository, search, turn = setup(tmp_path, max_context_turns=2)
    first = asyncio.run(
        turn(MemoryLLM(), "How to use trolley slot picking without tracking slot ID?")
    )
    llm = MemoryLLM()
    second = asyncio.run(turn(llm, "Where is that option?", first.session.session_id))
    assert search.queries[-1] == second.state["resolved_user_message"]
    assert "without tracking slot ID" in search.queries[-1]
    assert "Check the slot handling unit" in llm.prompts[1]
    assert "Where is that option?" in llm.prompts[1]
    assert second.state["memory_through_sequence"] == 1
    assert second.state["tokens_used"] == 60  # Summary + rewrite + grounded answer.
    assert second.state["answer_evidence"][0]["source"] == "slot.pdf"
    assert len(repository.list_turns(first.session.session_id)) == 4
    third = asyncio.run(turn(MemoryLLM(), "How should I check it?", first.session.session_id))
    assert third.state["memory_through_sequence"] == 3
    assert "without tracking slot ID" in third.state["conversation_summary"]
    assert len(repository.list_turns(first.session.session_id)) == 6


def test_other_session_never_receives_first_session_memory(tmp_path):
    _, _, turn = setup(tmp_path)
    asyncio.run(turn(MemoryLLM(), "How to use PRIVATE_SLOT_X without tracking slot ID?"))
    llm = MemoryLLM()
    other = asyncio.run(turn(llm, "Where is the receiving option?"))
    assert "PRIVATE_SLOT_X" not in str(llm.prompts)
    assert other.state["conversation_summary"] == ""


@pytest.mark.parametrize("failure", ["summary", "ambiguous", "size", "budget"])
def test_context_failures_preserve_records_and_never_retrieve_with_incomplete_memory(
    tmp_path, failure
):
    changes = {"max_context_turns": 2}
    if failure == "size":
        changes.update(max_context_chars=900, max_summary_chars=100)
    if failure == "budget":
        changes["max_tokens_per_turn"] = 100
    repository, search, turn = setup(tmp_path, **changes)
    first = asyncio.run(turn(MemoryLLM(), "How to configure a slot?"))
    llm = MemoryLLM(fail_summary=failure == "summary", ambiguous=failure == "ambiguous")
    message = "Where is it?" if failure != "size" else "x" * 1000
    second = asyncio.run(turn(llm, message, first.session.session_id))
    assert len(search.queries) == 1
    assert len(repository.list_turns(first.session.session_id)) == 4
    assert second.state["answer_evidence"] == []
    expected = {
        "summary": "memory_output_invalid",
        "ambiguous": "context_reference_invalid",
        "size": "context_limit_exceeded",
        "budget": "token_budget_exceeded",
    }
    assert second.state["pause_reason"] == expected[failure]
    if failure != "ambiguous":
        assert second.state["memory_through_sequence"] == 0
    if failure == "summary":
        recovered = asyncio.run(
            turn(MemoryLLM(), "Where is the slot option?", first.session.session_id)
        )
        assert recovered.state["pause_reason"] == "question_answered"
        assert len(search.queries) == 2


def test_old_assistant_claim_cannot_replace_documentary_evidence(tmp_path):
    from agents.llm_json import StructuredLLMError
    from agents.nodes.grounded_answer import answer_question

    class Fabricator:
        def chat(self, messages):
            return ChatResponse(
                json.dumps(
                    {
                        "status": "answered",
                        "gap": "",
                        "claims": [
                            {
                                "text": "Disable everything.",
                                "source_id": "1",
                                "quote": "Disable everything.",
                            }
                        ],
                    }
                )
            )

    evidence = (
        Evidence(
            evidence_id="e:1",
            chunk_id="c:1",
            source="manual.pdf",
            score=1,
            excerpt="Check the slot handling unit.",
        ),
    )
    with pytest.raises(StructuredLLMError):
        answer_question(
            Fabricator(),
            "What next?",
            evidence,
            conversation_context='Assistant previously said: "Disable everything."',
        )
