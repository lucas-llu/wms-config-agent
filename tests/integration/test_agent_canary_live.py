"""Opt-in live synthetic multi-turn acceptance, isolated from all user stores."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace

import pytest

from agents import ReviewDecision
from agents.repositories import SessionRepository
from agents.runtime import open_configured_checkpointer
from agents.services import SessionService, SolutionService, SolutionStateError
from agents.supervisor import RequirementSessionRunner, Supervisor
from agents.tools import KnowledgeSearchResult
from agents.workspace import Workspace, WorkspaceService
from core.settings import load_settings
from core.trace import TraceCollector
from libs.llm import LLMFactory


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("WMS_CANARY_LIVE") != "1", reason="set WMS_CANARY_LIVE=1")
def test_live_multiturn_missing_evidence_blocks_approval_and_export(tmp_path):
    settings = load_settings()
    agent = replace(
        settings.agent,
        workspace_id="workspace:canary",
        session_db_path=tmp_path / "sessions.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        export_root=tmp_path / "exports",
    )
    SessionRepository(agent.session_db_path)
    WorkspaceService(agent.session_db_path).create(
        Workspace(
            agent.workspace_id,
            "Synthetic canary",
            ("synthetic",),
            ("inbound", "appointment", "receiving", "integration"),
            ("TEST-A",),
            ("test",),
        )
    )

    class NoEvidence:
        calls = 0

        def search(self, query, *, filters, top_k=5, trace=None):
            self.calls += 1
            return KnowledgeSearchResult(query, filters, (), False, ())

    knowledge = NoEvidence()

    def runner():
        repository = SessionRepository(agent.session_db_path, workspace_id=agent.workspace_id)
        return RequirementSessionRunner(
            supervisor=Supervisor(
                llm=LLMFactory.create(settings),
                settings=agent,
                knowledge_adapter=knowledge,
                workspace=repository.workspace,
            ),
            sessions=SessionService(repository),
            trace_collector=TraceCollector(tmp_path / "traces.jsonl"),
        )

    def report(result):
        print(
            json.dumps(
                {
                    "revision": result.revision.revision,
                    "status": result.state.get("status"),
                    "pause_reason": result.state.get("pause_reason"),
                    "tokens_used": result.state.get("tokens_used"),
                    "tasks": len(result.state.get("configuration_tasks", [])),
                    "retrieval_calls": knowledge.calls,
                }
            ),
            flush=True,
        )

    async def run():
        async with open_configured_checkpointer(agent) as saver:
            first = await runner().start(
                "Build a receiving configuration plan. This is a synthetic test. "
                "The product version, warehouse and environment have not been provided.",
                checkpointer=saver,
                session_id="session:canary",
            )
        report(first)
        assert first.state["status"] == "paused"
        assert first.state.get("open_questions")
        async with open_configured_checkpointer(agent) as saver:
            second = await runner().continue_session(
                first.session.session_id,
                "Confirmed synthetic requirements: product_version DEMO-1, site TEST-A, "
                "environment test, business_process inbound receiving against an ASN, "
                "modules inbound. No integrations or customizations. Use only planning-level "
                "steps, at most two tasks. Product manuals are unavailable; do not invent "
                "commands or claim a verified configuration.",
                checkpointer=saver,
            )
        report(second)
        assert second.state["status"] == "paused"
        assert second.state.get("configuration_tasks")
        assert knowledge.calls > 0
        assert second.state.get("pause_reason") not in {
            "turn_timeout",
            "token_budget_exceeded",
            "retry_budget_exceeded",
        }
        assert second.state.get("tokens_used", 0) <= agent.max_tokens_per_turn
        return second

    result = asyncio.run(run())
    repository = SessionRepository(agent.session_db_path, workspace_id=agent.workspace_id)
    before = repository.get_revision(result.session.session_id)
    solutions = SolutionService(repository, agent.export_root)
    with pytest.raises(SolutionStateError):
        solutions.review(
            result.session.session_id,
            expected_revision=result.revision.revision,
            decision=ReviewDecision.APPROVE,
            actor="synthetic-test",
            comment="Must not approve without evidence",
        )
    with pytest.raises(SolutionStateError):
        solutions.export(
            result.session.session_id, expected_revision=result.revision.revision, format="markdown"
        )
    assert repository.get_revision(result.session.session_id) == before
    assert repository.list_approvals(result.session.session_id) == ()
    assert repository.list_exports(result.session.session_id) == ()
