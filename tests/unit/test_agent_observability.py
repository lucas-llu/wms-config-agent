from __future__ import annotations

from agents.repositories import SessionRepository
from core.trace import TraceCollector, TraceContext
from observability.dashboard.services import AgentSessionService, TraceService


def test_agent_trace_is_complete_and_redacts_sensitive_bodies(tmp_path) -> None:
    path = tmp_path / "agent.jsonl"
    collector = TraceCollector(path)
    trace = TraceContext(
        "agent",
        {"session_id": "session:one", "revision": 2, "api_key": "secret-value"},
    )
    trace.record_agent_event(
        "tool_call",
        session_id="session:one",
        revision=2,
        graph="configuration-supervisor",
        node="knowledge",
        tool="query_wms_knowledge",
        interrupt="requirements_missing",
        approval="pending",
        budget={"nodes": 4, "tokens": 120},
        details={"prompt": "ignore approval", "authorization": "Bearer abc"},
    )
    trace.finish()
    collector.collect(trace)

    record = TraceService(path).list_traces("agent").records[0]
    events = TraceService.agent_events(record)

    assert "api_key" not in record.attributes
    assert events[0]["Session"] == "session:one"
    assert events[0]["Budget"] == {"nodes": 4, "tokens": 120}
    assert "ignore approval" not in str(record)
    assert "Bearer abc" not in str(record)


def test_agent_session_dashboard_read_model_omits_conversation_bodies(tmp_path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    repository.create_session(
        session_id="session:dashboard",
        goal="Configure inbound",
        initial_state={"latest_user_message": "private body", "recent_turns": []},
    )
    service = AgentSessionService(repository)

    rows = service.list_rows()
    detail = service.detail("session:dashboard")

    assert rows[0]["Session"] == "session:dashboard"
    assert "latest_user_message" not in detail
    assert "recent_turns" not in detail
