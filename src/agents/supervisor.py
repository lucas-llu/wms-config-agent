"""Supervisor facade and durable multi-turn requirement-session runner."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from agents.budget import TurnBudgetPolicy
from agents.graph import AgentGraphState, SupervisorGraph
from agents.nodes import IntentClassifier, KnowledgeAgent, PlanningAgent, RequirementAgent
from agents.repositories import RevisionRecord, SessionRecord
from agents.runtime import session_checkpoint_config
from agents.services import SessionService, ValidationService
from agents.tools import KnowledgeAdapter
from core.settings import AgentSettings
from core.trace import TraceCollector, TraceContext
from libs.llm import BaseLLM


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    session: SessionRecord
    revision: RevisionRecord
    state: dict[str, Any]
    next_nodes: tuple[str, ...]
    interrupts: tuple[Any, ...]


class Supervisor:
    def __init__(
        self,
        *,
        llm: BaseLLM,
        settings: AgentSettings,
        knowledge_adapter: KnowledgeAdapter | None = None,
        clock: Any = time.time,
    ) -> None:
        self.settings = settings
        self.classifier = IntentClassifier(
            llm,
            confidence_threshold=settings.intent_confidence_threshold,
            max_retries=settings.max_self_repair_rounds,
            prompt_path=settings.intent_prompt_path,
        )
        self.requirement_agent = RequirementAgent(
            llm,
            max_retries=settings.max_self_repair_rounds,
            max_questions=settings.max_questions_per_turn,
            prompt_path=settings.requirement_prompt_path,
        )
        self.planning_agent = PlanningAgent(
            llm,
            max_retries=settings.max_self_repair_rounds,
            prompt_path=settings.planning_prompt_path,
            template_path=settings.planning_template_path,
        )
        self.knowledge_agent = (
            KnowledgeAgent(
                knowledge_adapter,
                max_retrieval_tasks=settings.max_retrieval_tasks,
            )
            if knowledge_adapter is not None
            else None
        )
        self.graph = SupervisorGraph(
            settings=settings,
            classifier=self.classifier,
            requirement_agent=self.requirement_agent,
            planning_agent=self.planning_agent,
            knowledge_agent=self.knowledge_agent,
            validation_service=ValidationService(),
            budget=TurnBudgetPolicy(settings, clock=clock),
        )

    def compile(self, checkpointer: BaseCheckpointSaver[Any]) -> Any:
        return self.graph.compile(checkpointer)


class RequirementSessionRunner:
    """Persist every completed or paused graph turn as a business revision."""

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        sessions: SessionService,
        trace_collector: TraceCollector | None = None,
        clock: Any = time.time,
    ) -> None:
        self.supervisor = supervisor
        self.sessions = sessions
        self.trace_collector = trace_collector
        self.clock = clock

    async def start(
        self,
        user_message: str,
        *,
        checkpointer: BaseCheckpointSaver[Any],
        session_id: str | None = None,
    ) -> WorkflowResult:
        session = self.sessions.create_session(user_message, session_id=session_id)
        turn = self.sessions.append_turn(
            session.session_id,
            expected_revision=session.current_revision,
            role="user",
            message=user_message,
        )
        graph = self.supervisor.compile(checkpointer)
        config = session_checkpoint_config(session.session_id)
        initial: AgentGraphState = {
            "session_id": session.session_id,
            "revision": session.current_revision,
            "status": session.status.value,
            "user_goal": user_message,
            "latest_user_message": user_message,
            "latest_turn_id": turn.turn_id,
            "recent_turns": [{"role": "user", "content": user_message}],
            "confirmed_context": {},
            "assumptions": [],
            "open_questions": [],
            "configuration_tasks": [],
            "dependency_edges": [],
            "invalidated_task_ids": [],
            "evidence_registry": [],
            "task_evidence_bindings": [],
            "conflicts": [],
            "validation_findings": [],
            "targeted_retrieval_requirements": {},
            "targeted_retrieval_rounds": 0,
            "nodes_executed": 0,
            "retry_count": 0,
            "tokens_used": 0,
            "tool_calls_made": 0,
            "turn_deadline_epoch": self.clock() + self.supervisor.settings.turn_timeout_seconds,
        }
        trace = await self._run_graph(
            graph, initial, config, session.session_id, session.current_revision
        )
        return await self._persist_result(graph, config, session.current_revision, trace)

    async def continue_session(
        self,
        session_id: str,
        user_message: str,
        *,
        checkpointer: BaseCheckpointSaver[Any],
    ) -> WorkflowResult:
        session = self.sessions.get_session(session_id)
        turn = self.sessions.append_turn(
            session_id,
            expected_revision=session.current_revision,
            role="user",
            message=user_message,
        )
        graph = self.supervisor.compile(checkpointer)
        config = session_checkpoint_config(session_id)
        command = Command(
            resume={"message": user_message, "turn_id": turn.turn_id},
            update={
                "revision": session.current_revision,
                "nodes_executed": 0,
                "retry_count": 0,
                "tokens_used": 0,
                "tool_calls_made": 0,
                "turn_deadline_epoch": self.clock() + self.supervisor.settings.turn_timeout_seconds,
            },
        )
        trace = await self._run_graph(graph, command, config, session_id, session.current_revision)
        return await self._persist_result(graph, config, session.current_revision, trace)

    async def _run_graph(
        self, graph: Any, input_value: Any, config: dict[str, Any], session_id: str, revision: int
    ) -> TraceContext | None:
        trace = (
            self.trace_collector.start(
                "agent",
                {
                    "session_id": session_id,
                    "revision": revision,
                    "graph": "configuration-supervisor",
                },
            )
            if self.trace_collector
            else None
        )
        async for event in graph.astream(input_value, config, stream_mode="updates"):
            if trace and isinstance(event, dict):
                for node, update in sorted(event.items()):
                    update_mapping = update if isinstance(update, dict) else {}
                    trace.record_agent_event(
                        "node_update",
                        session_id=session_id,
                        revision=revision,
                        graph="configuration-supervisor",
                        node=str(node),
                        budget={
                            key: update_mapping[key]
                            for key in (
                                "nodes_executed",
                                "retry_count",
                                "tokens_used",
                                "tool_calls_made",
                            )
                            if key in update_mapping
                        },
                        details={"updated_fields": sorted(update_mapping)},
                    )
        return trace

    async def _persist_result(
        self,
        graph: Any,
        config: dict[str, dict[str, str]],
        expected_revision: int,
        trace: TraceContext | None = None,
    ) -> WorkflowResult:
        snapshot = await graph.aget_state(config)
        values = dict(snapshot.values)
        revision = self.sessions.update_revision(
            values["session_id"],
            expected_revision=expected_revision,
            state_update=values,
            actor="supervisor",
            reason=str(values.get("pause_reason") or values.get("next_action") or "graph_turn"),
        )
        state = dict(values)
        state["revision"] = revision.revision
        interrupts = tuple(item.value for item in getattr(snapshot, "interrupts", ()))
        if trace:
            trace.record_agent_event(
                "checkpoint",
                session_id=values["session_id"],
                revision=revision.revision,
                graph="configuration-supervisor",
                interrupt=str(values.get("pause_reason") or "") if interrupts else None,
                budget={
                    key: values.get(key, 0)
                    for key in ("nodes_executed", "retry_count", "tokens_used", "tool_calls_made")
                },
                details={"status": values.get("status"), "next_nodes": list(snapshot.next)},
            )
            trace.finish(status="paused" if interrupts else "ok")
            if self.trace_collector:
                self.trace_collector.collect(trace)
        if values.get("status") == "paused" and values.get("open_questions"):
            question_text = "\n".join(
                str(item.get("text", "")).strip()
                for item in values["open_questions"]
                if str(item.get("text", "")).strip()
            )
            if question_text:
                self.sessions.append_turn(
                    values["session_id"],
                    expected_revision=revision.revision,
                    role="assistant",
                    message=question_text,
                    metadata={"kind": values.get("pause_reason", "clarification")},
                )
        return WorkflowResult(
            self.sessions.get_session(values["session_id"]),
            revision,
            state,
            tuple(snapshot.next),
            interrupts,
        )
