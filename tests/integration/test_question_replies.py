from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from agents import Evidence
from agents.repositories import SessionRepository
from agents.runtime import open_configured_checkpointer
from agents.services import SessionService
from agents.supervisor import RequirementSessionRunner, Supervisor
from agents.tools import KnowledgeSearchResult
from core.settings import load_settings


@pytest.mark.parametrize("mode", ["evidence", "empty", "failure"])
def test_questions_produce_persisted_reply_and_resume(tmp_path, mode):
    class NoLLM:
        def chat(self, *args, **kwargs):
            pytest.fail("Atomic question must use local evidence, not send excerpts to an LLM")

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
            return KnowledgeSearchResult(
                query, filters, evidence if mode == "evidence" else (), mode == "evidence", ()
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
            "manual.pdf" in reply and "Synthetic receiving rule" in reply for reply in replies
        )
    assert repository.list_approvals(result.session.session_id) == ()
