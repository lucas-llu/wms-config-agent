from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from langgraph.types import Command

from agents import Evidence
from agents.repositories import SessionRepository
from agents.runtime import open_configured_checkpointer, session_checkpoint_config
from agents.services import SessionService
from agents.supervisor import RequirementSessionRunner, Supervisor
from agents.tools import KnowledgeSearchResult
from core.settings import AgentSettings, load_settings
from core.trace import TraceCollector
from libs.llm import ChatResponse
from observability.dashboard.services import TraceService


class ScriptedLLM:
    model = "fake-workflow"

    def __init__(self, *outputs: dict[str, object] | str, tokens: int = 19) -> None:
        self.outputs = list(outputs)
        self.tokens = tokens
        self.messages = []

    def chat(self, messages, trace=None) -> ChatResponse:
        del trace
        self.messages.append(messages)
        output = self.outputs.pop(0)
        content = json.dumps(output) if isinstance(output, dict) else output
        return ChatResponse(
            content, model=self.model, metadata={"usage": {"total_tokens": self.tokens}}
        )


class FakeKnowledgeAdapter:
    def __init__(self) -> None:
        self.calls = []

    def search(self, query, *, filters, top_k=5, trace=None):
        del trace
        self.calls.append((query, filters, top_k))
        evidence = Evidence(
            evidence_id=f"evidence:{len(self.calls)}",
            chunk_id=f"chunk:{len(self.calls)}",
            source="sanitized/inbound.pdf",
            excerpt="Version-matched inbound configuration evidence.",
            score=0.9,
            product_version="2024.1",
            module=filters["module"],
        )
        return KnowledgeSearchResult(query, filters, (evidence,), True, ())


class UnsupportedKnowledgeAdapter(FakeKnowledgeAdapter):
    def search(self, query, *, filters, top_k=5, trace=None):
        del trace
        self.calls.append((query, filters, top_k))
        return KnowledgeSearchResult(query, filters, (), False, ())


def _settings(root: Path, **changes) -> AgentSettings:
    base = replace(
        load_settings().agent,
        checkpoint_path=root / "checkpoints.db",
        session_db_path=root / "sessions.db",
        export_root=root / "exports",
    )
    return replace(base, **changes)


def _requirement_outputs() -> tuple[dict[str, object], ...]:
    return (
        {
            "confirmed_context": {
                "business_process": "Inbound appointment",
                "modules": ["inbound"],
            },
            "assumptions": ["Standard volume may be sufficient"],
            "summary": "Configure inbound appointments",
        },
        {
            "confirmed_context": {"product_version": "2024.1"},
            "assumptions": [],
            "summary": "Target version confirmed",
        },
        {
            "confirmed_context": {"site": "DC01", "environment": "test"},
            "assumptions": [],
            "summary": "Site and environment confirmed",
        },
    )


def _planning_output() -> dict[str, object]:
    return {
        "tasks": [
            {
                "task_key": "confirm_scope",
                "title": "Confirm inbound appointment scope",
                "module": "inbound",
                "goal": "Confirm the site and environment scope",
                "depends_on": [],
                "preconditions": [],
                "steps": ["Review the confirmed inbound requirement baseline"],
                "validation_steps": ["Confirm DC01 and test are explicit"],
                "rollback_steps": ["Return the task to draft"],
                "evidence_requirements": ["Version-matched inbound flow documentation"],
                "risk_level": "low",
            },
            {
                "task_key": "plan_capacity",
                "title": "Plan appointment capacity",
                "module": "appointment",
                "goal": "Define appointment capacity behavior",
                "depends_on": ["confirm_scope"],
                "preconditions": ["Inbound scope confirmed"],
                "steps": ["Describe the required capacity behavior"],
                "validation_steps": ["Verify behavior against the confirmed goal"],
                "rollback_steps": ["Restore the previous capacity plan"],
                "evidence_requirements": ["Appointment capacity documentation"],
                "risk_level": "medium",
            },
        ]
    }


def test_three_turn_requirements_resume_across_restarts(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "agent", max_context_turns=2)
    llm = ScriptedLLM(*_requirement_outputs(), _planning_output())

    async def start_turn():
        sessions = SessionService(SessionRepository(settings.session_db_path))
        runner = RequirementSessionRunner(
            supervisor=Supervisor(llm=llm, settings=settings), sessions=sessions
        )
        async with open_configured_checkpointer(settings) as checkpointer:
            return await runner.start(
                "Build a complete inbound appointment configuration plan",
                checkpointer=checkpointer,
                session_id="session:three-turn",
            )

    async def continue_turn(message: str):
        sessions = SessionService(SessionRepository(settings.session_db_path))
        runner = RequirementSessionRunner(
            supervisor=Supervisor(llm=llm, settings=settings), sessions=sessions
        )
        async with open_configured_checkpointer(settings) as checkpointer:
            return await runner.continue_session(
                "session:three-turn", message, checkpointer=checkpointer
            )

    first = asyncio.run(start_turn())
    second = asyncio.run(continue_turn("The product version is 2024.1"))
    third = asyncio.run(continue_turn("Use site DC01 in the test environment"))
    repository = SessionRepository(settings.session_db_path)

    assert first.state["status"] == "paused"
    assert first.next_nodes == ("await_requirements",)
    assert len(first.interrupts) == 1
    assert all(item["confirmed"] is False for item in first.state["assumptions"])
    assert "volume_profile" not in first.state["confirmed_context"]
    second_reasons = {item["reason"] for item in second.state["open_questions"]}
    assert "required_context_missing:product_version" not in second_reasons
    assert third.state["status"] == "retrieving"
    assert third.state["next_action"] == "retrieve_evidence"
    assert third.state["confirmed_context"] == {
        "business_process": "Inbound appointment",
        "modules": ["inbound"],
        "product_version": "2024.1",
        "site": "DC01",
        "environment": "test",
    }
    assert len(third.state["recent_turns"]) == 2
    assert [task["title"] for task in third.state["configuration_tasks"]] == [
        "Confirm inbound appointment scope",
        "Plan appointment capacity",
    ]
    assert (
        third.state["dependency_edges"][0]["upstream_task_id"]
        == third.state["configuration_tasks"][0]["task_id"]
    )
    assert (
        third.state["dependency_edges"][0]["downstream_task_id"]
        == third.state["configuration_tasks"][1]["task_id"]
    )
    assert third.state["planning_baseline_fingerprint"].startswith("baseline:")
    assert third.session.current_revision == 4
    assert [item.revision for item in repository.list_revisions("session:three-turn")] == [
        1,
        2,
        3,
        4,
    ]
    assert [item.role for item in repository.list_turns("session:three-turn")] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert third.state["open_questions"] == []
    assert "2 tasks" in repository.list_turns("session:three-turn")[-1].message
    assert "not an executable or approved" in third.state["assistant_reply"]


def test_completed_plan_collects_task_evidence_before_validation(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "knowledge")
    trace_path = tmp_path / "knowledge" / "agent-traces.jsonl"
    llm = ScriptedLLM(
        {
            "confirmed_context": {
                "business_process": "Inbound appointment",
                "modules": ["inbound", "appointment"],
                "product_version": "2024.1",
                "site": "DC01",
                "environment": "test",
            },
            "assumptions": [],
            "summary": "Complete inbound scope",
        },
        _planning_output(),
    )
    adapter = FakeKnowledgeAdapter()

    async def scenario():
        sessions = SessionService(SessionRepository(settings.session_db_path))
        runner = RequirementSessionRunner(
            supervisor=Supervisor(
                llm=llm,
                settings=settings,
                knowledge_adapter=adapter,  # type: ignore[arg-type]
            ),
            sessions=sessions,
            trace_collector=TraceCollector(trace_path),
        )
        async with open_configured_checkpointer(settings) as checkpointer:
            return await runner.start(
                "Build a complete inbound appointment configuration plan",
                checkpointer=checkpointer,
                session_id="session:knowledge",
            )

    result = asyncio.run(scenario())

    assert result.state["status"] == "review_required"
    assert result.state["next_action"] == "compose_draft"
    assert result.state["knowledge_fingerprint"].startswith("knowledge:")
    assert [item["evidence_status"] for item in result.state["task_evidence_bindings"]] == [
        "supported",
        "supported",
    ]
    assert len(result.state["evidence_registry"]) == 2
    assert result.state["tool_calls_made"] == 2
    assert result.state["conflicts"] == []
    assert result.state["validation_findings"] == []
    assert all(call[1]["version"] == "2024.1" for call in adapter.calls)
    trace = TraceService(trace_path).list_traces("agent").records[0]
    events = TraceService.agent_events(trace)
    nodes = {item["Node"] for item in events if item["Event"] == "node_update"}
    assert {
        "classify_intent",
        "extract_requirements",
        "plan_tasks",
        "retrieve_evidence",
        "analyze_conflicts",
        "validate_draft",
        "complete_validation",
    } <= nodes
    assert any(item["Event"] == "checkpoint" for item in events)
    assert "not approved" in result.state["assistant_reply"]
    sessions = SessionRepository(settings.session_db_path).list_turns("session:knowledge")
    assert len(sessions) == 2
    assert sessions[-1].role == "assistant"


def _chinese_plan() -> dict[str, object]:
    plan = _planning_output()
    for task in plan["tasks"]:
        task["title"] = "确认入库范围" if task["task_key"] == "confirm_scope" else "规划预约容量"
        task["goal"] = "核验已确认的配置需求"
        for field in ("steps", "validation_steps", "rollback_steps", "evidence_requirements"):
            task[field] = ["按对应文档检查并确认配置条件"]
        task["preconditions"] = ["入库范围已确认"] if task["depends_on"] else []
    return plan


def test_chinese_clarification_across_restarts_produces_visible_final_reply(tmp_path):
    settings = _settings(tmp_path / "zh")
    llm = ScriptedLLM(*_requirement_outputs(), _chinese_plan())
    repository = SessionRepository(settings.session_db_path)

    async def turn(message, first=False):
        runner = RequirementSessionRunner(
            supervisor=Supervisor(
                llm=llm, settings=settings, knowledge_adapter=FakeKnowledgeAdapter()
            ),
            sessions=SessionService(repository),
        )
        async with open_configured_checkpointer(settings) as saver:
            if first:
                return await runner.start(message, checkpointer=saver, session_id="session:zh")
            return await runner.continue_session("session:zh", message, checkpointer=saver)

    first = asyncio.run(turn("帮我配置一个入库预约完整方案", True))
    assert all("？" in q["text"] for q in first.state["open_questions"])
    second = asyncio.run(turn("2024.1"))
    assert second.state["response_language"] == "zh"
    assert len(second.state["open_questions"]) == 2
    assert "产品版本" not in second.state["assistant_reply"]
    third = asyncio.run(turn("DC01，测试环境"))
    assert third.state["status"] == "review_required"
    assert third.state["open_questions"] == []
    assert "已生成 2 项配置任务" in third.state["assistant_reply"]
    assert "尚未批准" in third.state["assistant_reply"]
    assert [t.role for t in repository.list_turns("session:zh")] == ["user", "assistant"] * 3
    assert repository.list_approvals("session:zh") == ()
    assert all("Response language: Chinese" in m[0]["content"] for m in llm.messages)


@pytest.mark.parametrize("changed_baseline", [False, True])
def test_validation_clarification_replans_and_returns_result_without_approval(
    tmp_path, changed_baseline
):
    settings = _settings(tmp_path / "validation-resume")
    initial_context = {
        "business_process": "Inbound appointment",
        "modules": ["inbound", "appointment"],
        "product_version": "2024.1",
        "site": "DC01",
        "environment": "test",
    }
    llm = ScriptedLLM(
        {"confirmed_context": initial_context, "assumptions": [], "summary": "Complete scope"},
        _planning_output(),
        {
            "confirmed_context": {"constraints": ["Use the standard flow"]}
            if changed_baseline
            else {},
            "assumptions": [],
            "summary": "Revised scope",
        },
        _planning_output(),
    )
    repository = SessionRepository(settings.session_db_path)

    async def run():
        async with open_configured_checkpointer(settings) as saver:
            first = await RequirementSessionRunner(
                supervisor=Supervisor(
                    llm=llm, settings=settings, knowledge_adapter=UnsupportedKnowledgeAdapter()
                ),
                sessions=SessionService(repository),
            ).start("Build a complete inbound configuration plan", checkpointer=saver)
        assert first.state["pause_reason"] == "validation_blocked"
        assert "validation is blocked" in first.state["assistant_reply"]
        async with open_configured_checkpointer(settings) as saver:
            adapter = FakeKnowledgeAdapter()
            second = await RequirementSessionRunner(
                supervisor=Supervisor(llm=llm, settings=settings, knowledge_adapter=adapter),
                sessions=SessionService(repository),
            ).continue_session(
                first.session.session_id, "Use the standard flow", checkpointer=saver
            )
        return second, adapter

    result, adapter = asyncio.run(run())
    assert result.state["status"] == ("paused" if changed_baseline else "review_required")
    assert result.state["pause_reason"] == ("validation_blocked" if changed_baseline else "")
    assert result.state["targeted_retrieval_rounds"] == 0
    assert result.state["confirmed_context"]["site"] == "DC01"
    assert len(adapter.calls) == 2
    if changed_baseline:
        assert result.state["invalidated_task_ids"]
        assert "validation is blocked" in result.state["assistant_reply"]
        assert any(f["rule_id"] == "invalidated_tasks" for f in result.state["validation_findings"])
    else:
        assert result.state["confirmed_context"] == initial_context
        assert "no blocking validation findings" in result.state["assistant_reply"]
    assert len(repository.list_turns(result.session.session_id)) == 4
    assert repository.list_approvals(result.session.session_id) == ()


def test_noninterrupt_budget_pause_can_resume_without_replaying_old_state(tmp_path):
    settings = _settings(tmp_path / "budget-resume", max_nodes_per_turn=1)
    repository = SessionRepository(settings.session_db_path)

    async def run():
        async with open_configured_checkpointer(settings) as saver:
            first = await RequirementSessionRunner(
                supervisor=Supervisor(llm=ScriptedLLM(), settings=settings),
                sessions=SessionService(repository),
            ).start("Build a complete inbound configuration plan", checkpointer=saver)
        assert not first.interrupts
        assert "This turn paused" in first.state["assistant_reply"]
        normal = replace(settings, max_nodes_per_turn=20)
        async with open_configured_checkpointer(normal) as saver:
            return await RequirementSessionRunner(
                supervisor=Supervisor(llm=ScriptedLLM(_requirement_outputs()[0]), settings=normal),
                sessions=SessionService(repository),
            ).continue_session(
                first.session.session_id,
                "Build a complete inbound configuration plan",
                checkpointer=saver,
            )

    result = asyncio.run(run())
    assert result.state["pause_reason"] == "requirements_missing"
    assert result.next_nodes == ("await_requirements",)
    assert "please confirm" in result.state["assistant_reply"]
    assert len(repository.list_turns(result.session.session_id)) == 4


def test_evidence_gaps_retry_twice_then_interrupt_before_review(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "validation-gap")
    llm = ScriptedLLM(
        {
            "confirmed_context": {
                "business_process": "Inbound appointment",
                "modules": ["inbound", "appointment"],
                "product_version": "2024.1",
                "site": "DC01",
                "environment": "test",
            },
            "assumptions": [],
            "summary": "Complete inbound scope",
        },
        _planning_output(),
    )
    adapter = UnsupportedKnowledgeAdapter()

    async def scenario():
        sessions = SessionService(SessionRepository(settings.session_db_path))
        runner = RequirementSessionRunner(
            supervisor=Supervisor(
                llm=llm,
                settings=settings,
                knowledge_adapter=adapter,  # type: ignore[arg-type]
            ),
            sessions=sessions,
        )
        async with open_configured_checkpointer(settings) as checkpointer:
            return await runner.start(
                "Build a complete inbound appointment configuration plan",
                checkpointer=checkpointer,
                session_id="session:validation-gap",
            )

    result = asyncio.run(scenario())

    assert result.state["status"] == "paused"
    assert result.state["pause_reason"] == "validation_blocked"
    assert result.state["targeted_retrieval_rounds"] == 2
    assert result.state["next_action"] == "ask_user"
    assert result.next_nodes == ("await_validation",)
    assert len(result.interrupts) == 1
    assert len(adapter.calls) == 6
    assert all(
        item["evidence_status"] == "unsupported" for item in result.state["task_evidence_bindings"]
    )


def test_low_confidence_intent_interrupts_and_resumes(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "agent")
    llm = ScriptedLLM(
        {"intent": "configure_goal", "confidence": 0.4, "reason": "ambiguous"},
        {
            "confirmed_context": {},
            "assumptions": [],
            "summary": "Configuration plan requested",
        },
    )

    async def scenario():
        supervisor = Supervisor(llm=llm, settings=settings)
        config = session_checkpoint_config("session:intent")
        async with open_configured_checkpointer(settings) as checkpointer:
            graph = supervisor.compile(checkpointer)
            initial = {
                "session_id": "session:intent",
                "revision": 1,
                "status": "created",
                "latest_user_message": "I need warehouse help",
                "latest_turn_id": "turn:intent-one",
                "recent_turns": [],
                "confirmed_context": {},
                "assumptions": [],
                "open_questions": [],
                "nodes_executed": 0,
                "retry_count": 0,
                "tokens_used": 0,
                "turn_deadline_epoch": time.time() + 60,
            }
            async for _event in graph.astream(initial, config, stream_mode="updates"):
                pass
            first = await graph.aget_state(config)
            async for _event in graph.astream(
                Command(resume={"message": "Build a complete receiving configuration plan"}),
                config,
                stream_mode="updates",
            ):
                pass
            second = await graph.aget_state(config)
        return first, second

    first, second = asyncio.run(scenario())

    assert first.next == ("await_intent",)
    assert first.values["pause_reason"] == "intent_clarification"
    assert second.next == ("await_requirements",)
    assert second.values["intent"] == "configure_goal"


def test_node_and_token_budgets_pause_without_looping(tmp_path: Path) -> None:
    node_settings = _settings(tmp_path / "node", max_nodes_per_turn=1)
    token_settings = _settings(tmp_path / "token", max_tokens_per_turn=1)

    async def run(settings: AgentSettings, llm: ScriptedLLM, session_id: str):
        supervisor = Supervisor(llm=llm, settings=settings)
        config = session_checkpoint_config(session_id)
        async with open_configured_checkpointer(settings) as checkpointer:
            graph = supervisor.compile(checkpointer)
            async for _event in graph.astream(
                {
                    "session_id": session_id,
                    "revision": 1,
                    "status": "created",
                    "latest_user_message": "Build a complete inbound configuration plan",
                    "latest_turn_id": f"turn:{session_id}",
                    "recent_turns": [],
                    "confirmed_context": {},
                    "assumptions": [],
                    "open_questions": [],
                    "nodes_executed": 0,
                    "retry_count": 0,
                    "tokens_used": 0,
                    "turn_deadline_epoch": time.time() + 60,
                },
                config,
                stream_mode="updates",
            ):
                pass
            return await graph.aget_state(config)

    node_result = asyncio.run(run(node_settings, ScriptedLLM(), "session:node-budget"))
    token_result = asyncio.run(
        run(
            token_settings,
            ScriptedLLM(
                {
                    "confirmed_context": {},
                    "assumptions": [],
                    "summary": "Token budget test",
                },
                tokens=20,
            ),
            "session:token-budget",
        )
    )

    assert node_result.next == ()
    assert node_result.values["pause_reason"] == "node_budget_exceeded"
    assert token_result.next == ()
    assert token_result.values["pause_reason"] == "token_budget_exceeded"


def test_non_configuration_intents_route_to_terminal_response_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "agent")

    async def scenario():
        supervisor = Supervisor(llm=ScriptedLLM(), settings=settings)
        results = []
        async with open_configured_checkpointer(settings) as checkpointer:
            graph = supervisor.compile(checkpointer)
            for index, message in enumerate(
                (
                    "Where is appointment capacity configured?",
                    "Show the current configuration draft",
                    "Tell me a weather joke",
                ),
                start=1,
            ):
                config = session_checkpoint_config(f"session:route-{index}")
                async for _event in graph.astream(
                    {
                        "session_id": f"session:route-{index}",
                        "status": "created",
                        "latest_user_message": message,
                        "nodes_executed": 0,
                        "retry_count": 0,
                        "tokens_used": 0,
                        "turn_deadline_epoch": time.time() + 60,
                    },
                    config,
                    stream_mode="updates",
                ):
                    pass
                results.append(await graph.aget_state(config))
        return results

    results = asyncio.run(scenario())

    assert [(item.values["intent"], item.values["next_action"]) for item in results] == [
        ("atomic_query", "query_knowledge"),
        ("inspect_draft", "render_current_draft"),
        ("unsupported", "bounded_rejection"),
    ]
    assert all(item.next == ("await_question",) for item in results)


def test_exhausted_structured_output_retries_pause_safely(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "agent", max_self_repair_rounds=2)

    async def scenario():
        supervisor = Supervisor(
            llm=ScriptedLLM("bad-json", "bad-json", "bad-json"), settings=settings
        )
        config = session_checkpoint_config("session:invalid-output")
        async with open_configured_checkpointer(settings) as checkpointer:
            graph = supervisor.compile(checkpointer)
            async for _event in graph.astream(
                {
                    "session_id": "session:invalid-output",
                    "status": "created",
                    "latest_user_message": "warehouse assistance",
                    "nodes_executed": 0,
                    "retry_count": 0,
                    "tokens_used": 0,
                    "turn_deadline_epoch": time.time() + 60,
                },
                config,
                stream_mode="updates",
            ):
                pass
            return await graph.aget_state(config)

    result = asyncio.run(scenario())

    assert result.next == ()
    assert result.values["status"] == "paused"
    assert result.values["pause_reason"] == "intent_output_invalid"
    assert result.values["active_agent"] == "supervisor"
    assert result.values["retry_count"] == 2
