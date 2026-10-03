from __future__ import annotations

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


@pytest.mark.parametrize("mode", ["evidence", "empty", "failure", "evidence_then_empty"])
def test_questions_produce_persisted_reply_and_resume(tmp_path, mode):
    class NoLLM:
        def chat(self, *args, **kwargs):
            assert mode in {"evidence", "evidence_then_empty"}
            return ChatResponse(
                json.dumps(
                    {
                        "status": "answered",
                        "gap": "",
                        "claims": [
                            {
                                "text": "Synthetic receiving rule",
                                "source_id": "1",
                                "quote": "Synthetic receiving rule",
                            }
                        ],
                    }
                ),
                metadata={"usage": {"total_tokens": 30}},
            )

    class Search:
        calls = 0

        def search(self, query, *, filters, top_k=5):
            self.calls += 1
            if mode == "failure":
                raise RuntimeError("private failure details")
            evidence = (
                Evidence(
                    evidence_id="e:1",
                    chunk_id="c:1",
                    source="manual.pdf",
                    excerpt="Synthetic receiving rule",
                    score=1.0,
                    page_start=2,
                ),
            )
            supported = mode == "evidence" or (mode == "evidence_then_empty" and self.calls == 1)
            return KnowledgeSearchResult(
                query, filters, evidence if supported else (), supported, ()
            )

    settings = replace(load_settings().agent, checkpoint_path=tmp_path / "graph.db")
    repository = SessionRepository(tmp_path / "sessions.db")
    search = Search()

    def runner():
        return RequirementSessionRunner(
            supervisor=Supervisor(llm=NoLLM(), settings=settings, knowledge_adapter=search),
            sessions=SessionService(repository),
        )

    async def run():
        async with open_configured_checkpointer(settings) as saver:
            first = await runner().start("What is the receiving rule?", checkpointer=saver)
        assert first.state["pause_reason"] == "question_answered"
        assert first.next_nodes == ("await_question",)
        async with open_configured_checkpointer(settings) as saver:
            second = await runner().continue_session(
                first.session.session_id, "Where is the ASN rule?", checkpointer=saver
            )
        return second

    result = asyncio.run(run())
    replies = [
        t.message for t in repository.list_turns(result.session.session_id) if t.role == "assistant"
    ]
    assert len(replies) == 2
    assert search.calls == 2
    assert "private failure" not in str(replies)
    assert all(replies)
    if mode == "evidence":
        assert all(
            "manual.pdf" not in reply and "Synthetic receiving rule" in reply for reply in replies
        )
        citations = repository.get_revision(result.session.session_id).state["answer_evidence"]
        assert citations[0]["citation_index"] == 1
        assert citations[0]["source"] == "manual.pdf"
        turns = [
            t for t in repository.list_turns(result.session.session_id) if t.role == "assistant"
        ]
        assert all(t.metadata["citations"][0]["source"] == "manual.pdf" for t in turns)
        assert all(
            t.metadata["citations"][0]["supporting_quotes"] == ["Synthetic receiving rule"]
            for t in turns
        )
        assert result.state.get("evidence_registry", []) == []
    else:
        assert result.state["answer_evidence"] == []
    if mode == "evidence_then_empty":
        assert repository.get_revision(result.session.session_id, 2).state["answer_evidence"]
    assert repository.list_approvals(result.session.session_id) == ()
