from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langgraph.types import Command

from agents.repositories import SessionRepository
from agents.runtime import (
    build_runtime_probe_graph,
    open_configured_checkpointer,
    session_checkpoint_config,
)
from agents.services import SessionService
from core.settings import AgentSettings


def _settings(root: Path) -> AgentSettings:
    return AgentSettings(
        enabled=False,
        runtime="langgraph",
        checkpoint_path=root / "checkpoints.db",
        session_db_path=root / "sessions.db",
        export_root=root / "exports",
        max_nodes_per_turn=12,
        max_self_repair_rounds=2,
        max_retrieval_tasks=8,
        turn_timeout_seconds=60,
        max_context_turns=8,
        approval_required=True,
        environment_inspector_enabled=False,
    )


def test_business_session_id_resumes_the_matching_checkpoint_after_restart(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "agent")
    service = SessionService(SessionRepository(settings.session_db_path))
    session = service.create_session("Configure inbound", session_id="session:checkpoint")

    async def scenario() -> dict[str, object]:
        config = session_checkpoint_config(session.session_id)
        async with open_configured_checkpointer(settings) as checkpointer:
            graph = build_runtime_probe_graph(checkpointer)
            async for _event in graph.astream(
                {"subject": "session-checkpoint"}, config, stream_mode="updates"
            ):
                pass
            paused = await graph.aget_state(config)
        async with open_configured_checkpointer(settings) as checkpointer:
            graph = build_runtime_probe_graph(checkpointer)
            async for _event in graph.astream(
                Command(resume={"approved": True}), config, stream_mode="updates"
            ):
                pass
            completed = await graph.aget_state(config)
        return {
            "paused_next": paused.next,
            "completed_next": completed.next,
            "values": dict(completed.values),
        }

    result = asyncio.run(scenario())
    restarted_repository = SessionRepository(settings.session_db_path)

    assert session.checkpoint_thread_id == session.session_id
    assert result["paused_next"] == ("approval",)
    assert result["completed_next"] == ()
    assert result["values"]["result"] == "approved"
    assert restarted_repository.get_session(session.session_id).current_revision == 1
    assert settings.checkpoint_path.is_file()
    assert settings.session_db_path.is_file()
    assert settings.checkpoint_path != settings.session_db_path


def test_configured_checkpointer_rejects_business_database_reuse(tmp_path: Path) -> None:
    database = tmp_path / "shared.db"
    settings = AgentSettings(
        enabled=False,
        runtime="langgraph",
        checkpoint_path=database,
        session_db_path=database,
        export_root=tmp_path / "exports",
        max_nodes_per_turn=12,
        max_self_repair_rounds=2,
        max_retrieval_tasks=8,
        turn_timeout_seconds=60,
        max_context_turns=8,
        approval_required=True,
        environment_inspector_enabled=False,
    )

    async def scenario() -> None:
        async with open_configured_checkpointer(settings):
            raise AssertionError("the invalid checkpointer context must not open")

    with pytest.raises(ValueError, match="must be separate"):
        asyncio.run(scenario())


def test_session_checkpoint_config_requires_explicit_session_id() -> None:
    assert session_checkpoint_config("session:one") == {
        "configurable": {"thread_id": "session:one"}
    }
    with pytest.raises(ValueError, match="session_id"):
        session_checkpoint_config(" ")
