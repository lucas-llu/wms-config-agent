"""Explicit LangGraph supervisor workflow for intent and requirement collection."""

from __future__ import annotations

from types import MappingProxyType
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agents.budget import TurnBudgetPolicy
from agents.contracts import IntentType, OpenQuestion, SessionStatus, stable_contract_id
from agents.llm_json import StructuredLLMError
from agents.nodes import IntentClassifier, RequirementAgent
from core.settings import AgentSettings


def merge_context(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def merge_assumptions(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in [*(left or []), *(right or [])]:
        identifier = str(item.get("assumption_id", ""))
        if identifier:
            merged[identifier] = dict(item)
    return list(merged.values())


class AgentGraphState(TypedDict, total=False):
    session_id: str
    revision: int
    status: str
    created_at: str
    updated_at: str
    user_goal: str
    intent: str
    intent_confidence: float
    intent_reason: str
    intent_needs_clarification: bool
    active_agent: str
    next_action: str
    pause_reason: str
    latest_user_message: str
    latest_turn_id: str
    recent_turns: list[dict[str, str]]
    requirement_summary: str
    confirmed_context: Annotated[dict[str, Any], merge_context]
    assumptions: Annotated[list[dict[str, Any]], merge_assumptions]
    open_questions: list[dict[str, Any]]
    nodes_executed: int
    retry_count: int
    tokens_used: int
    tool_calls_made: int
    turn_deadline_epoch: float
    trace_id: str


ALLOWED_TRANSITIONS = MappingProxyType(
    {
        SessionStatus.CREATED: frozenset(
            {SessionStatus.COLLECTING_REQUIREMENTS, SessionStatus.PAUSED, SessionStatus.FAILED}
        ),
        SessionStatus.COLLECTING_REQUIREMENTS: frozenset(
            {
                SessionStatus.COLLECTING_REQUIREMENTS,
                SessionStatus.PAUSED,
                SessionStatus.PLANNING,
                SessionStatus.CANCELLED,
                SessionStatus.FAILED,
            }
        ),
        SessionStatus.PAUSED: frozenset(
            {
                SessionStatus.CREATED,
                SessionStatus.COLLECTING_REQUIREMENTS,
                SessionStatus.CANCELLED,
                SessionStatus.FAILED,
            }
        ),
    }
)


def transition_status(state: dict[str, Any], target: SessionStatus) -> str:
    current = SessionStatus(str(state.get("status", SessionStatus.CREATED.value)))
    if current is target:
        return target.value
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"Invalid Agent session transition: {current.value} -> {target.value}")
    return target.value


class SupervisorGraph:
    """Build a bounded, recoverable intent and requirement workflow."""

    def __init__(
        self,
        *,
        settings: AgentSettings,
        classifier: IntentClassifier,
        requirement_agent: RequirementAgent,
        budget: TurnBudgetPolicy,
    ) -> None:
        self.settings = settings
        self.classifier = classifier
        self.requirement_agent = requirement_agent
        self.budget = budget

    def compile(self, checkpointer: BaseCheckpointSaver[Any]) -> Any:
        builder = StateGraph(AgentGraphState)
        builder.add_node("classify_intent", self._classify_intent)
        builder.add_node("pause_intent", self._pause_intent)
        builder.add_node("await_intent", self._await_intent)
        builder.add_node("extract_requirements", self._extract_requirements)
        builder.add_node("pause_requirements", self._pause_requirements)
        builder.add_node("await_requirements", self._await_requirements)
        builder.add_node("complete_requirements", self._complete_requirements)
        builder.add_edge(START, "classify_intent")
        builder.add_conditional_edges(
            "classify_intent",
            self._route_after_classification,
            {
                "clarify": "pause_intent",
                "configure": "extract_requirements",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "pause_intent", self._route_pause, {"await": "await_intent", "end": END}
        )
        builder.add_conditional_edges(
            "await_intent",
            self._route_after_await,
            {"continue": "classify_intent", "end": END},
        )
        builder.add_conditional_edges(
            "extract_requirements",
            self._route_after_requirements,
            {
                "missing": "pause_requirements",
                "complete": "complete_requirements",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "pause_requirements",
            self._route_pause,
            {"await": "await_requirements", "end": END},
        )
        builder.add_conditional_edges(
            "await_requirements",
            self._route_after_await,
            {"continue": "extract_requirements", "end": END},
        )
        builder.add_edge("complete_requirements", END)
        return builder.compile(checkpointer=checkpointer, name="configuration-supervisor")

    def _classify_intent(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "supervisor")
        if not entered.allowed:
            return entered.update
        try:
            result = self.classifier.classify(state["latest_user_message"])
        except StructuredLLMError as exc:
            return self._structured_failure(
                state, entered.update, exc, "intent_output_invalid", "supervisor"
            )
        accounted = self.budget.account_llm(
            state,
            retries=result.retries,
            tokens_used=result.tokens_used,
            node_name="supervisor",
        )
        update = {**entered.update, **accounted.update}
        if not accounted.allowed:
            return update
        update.update(
            {
                "intent": result.intent.value,
                "intent_confidence": result.confidence,
                "intent_reason": result.reason,
                "intent_needs_clarification": self.classifier.requires_clarification(result),
                "active_agent": "supervisor",
                "next_action": "route_intent",
                "pause_reason": "",
            }
        )
        return update

    def _pause_intent(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "supervisor")
        if not entered.allowed:
            return entered.update
        question = OpenQuestion(
            question_id=stable_contract_id("question", {"field": "intent"}),
            text="Are you asking a one-time question or building a complete configuration plan?",
            reason="intent_confidence_below_threshold",
        )
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.PAUSED),
            "pause_reason": "intent_clarification",
            "next_action": "ask_user",
            "open_questions": [question.to_dict()],
        }

    def _await_intent(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "supervisor")
        if not entered.allowed:
            return entered.update
        response = interrupt(
            {"kind": "intent_clarification", "questions": state.get("open_questions", [])}
        )
        message, turn_id = _resume_message(response)
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.CREATED),
            "latest_user_message": message,
            "latest_turn_id": turn_id,
            "recent_turns": _append_recent_turn(
                state.get("recent_turns", []), message, self.settings.max_context_turns
            ),
            "open_questions": [],
            "intent_needs_clarification": False,
            "pause_reason": "",
            "next_action": "classify_intent",
        }

    def _extract_requirements(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "requirement")
        if not entered.allowed:
            return entered.update
        try:
            result = self.requirement_agent.extract(
                user_message=state["latest_user_message"],
                turn_id=state["latest_turn_id"],
                confirmed_context=dict(state.get("confirmed_context", {})),
                recent_turns=list(state.get("recent_turns", [])),
                requirement_summary=str(state.get("requirement_summary", "")),
            )
        except StructuredLLMError as exc:
            return self._structured_failure(
                state, entered.update, exc, "requirement_output_invalid", "requirement"
            )
        accounted = self.budget.account_llm(
            state,
            retries=result.retries,
            tokens_used=result.tokens_used,
            node_name="requirement",
        )
        update = {**entered.update, **accounted.update}
        if not accounted.allowed:
            return update
        update.update(
            {
                "status": transition_status(state, SessionStatus.COLLECTING_REQUIREMENTS),
                "active_agent": "requirement",
                "confirmed_context": result.confirmed_context,
                "assumptions": [item.to_dict() for item in result.assumptions],
                "open_questions": [item.to_dict() for item in result.open_questions],
                "requirement_summary": result.summary,
                "next_action": "check_requirement_gaps",
                "pause_reason": "",
            }
        )
        return update

    def _pause_requirements(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "requirement")
        if not entered.allowed:
            return entered.update
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.PAUSED),
            "pause_reason": "requirements_missing",
            "next_action": "ask_user",
        }

    def _await_requirements(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "requirement")
        if not entered.allowed:
            return entered.update
        response = interrupt(
            {"kind": "requirement_clarification", "questions": state.get("open_questions", [])}
        )
        message, turn_id = _resume_message(response)
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.COLLECTING_REQUIREMENTS),
            "latest_user_message": message,
            "latest_turn_id": turn_id,
            "recent_turns": _append_recent_turn(
                state.get("recent_turns", []), message, self.settings.max_context_turns
            ),
            "open_questions": [],
            "pause_reason": "",
            "next_action": "extract_requirements",
        }

    def _complete_requirements(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "supervisor")
        if not entered.allowed:
            return entered.update
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.PLANNING),
            "active_agent": "supervisor",
            "next_action": "plan_tasks",
            "pause_reason": "",
        }

    def _structured_failure(
        self,
        state: AgentGraphState,
        entered_update: dict[str, Any],
        error: StructuredLLMError,
        reason: str,
        node_name: str,
    ) -> dict[str, Any]:
        accounted = self.budget.account_llm(
            state,
            retries=error.retries,
            tokens_used=error.tokens_used,
            node_name=node_name,
        )
        return {
            **entered_update,
            **accounted.update,
            "status": SessionStatus.PAUSED.value,
            "pause_reason": reason,
            "next_action": "retry_or_change_provider",
            "active_agent": node_name,
        }

    @staticmethod
    def _route_after_classification(state: AgentGraphState) -> str:
        if _is_budget_or_failure_pause(state):
            return "end"
        if state.get("intent_needs_clarification"):
            return "clarify"
        if state.get("intent") == IntentType.CONFIGURE_GOAL.value:
            return "configure"
        return "end"

    @staticmethod
    def _route_after_requirements(state: AgentGraphState) -> str:
        if _is_budget_or_failure_pause(state):
            return "end"
        return "missing" if state.get("open_questions") else "complete"

    @staticmethod
    def _route_pause(state: AgentGraphState) -> str:
        return (
            "await"
            if state.get("pause_reason") in {"intent_clarification", "requirements_missing"}
            else "end"
        )

    @staticmethod
    def _route_after_await(state: AgentGraphState) -> str:
        return "end" if _is_budget_or_failure_pause(state) else "continue"


def _resume_message(response: Any) -> tuple[str, str]:
    if isinstance(response, dict):
        message = str(response.get("message", "")).strip()
        turn_id = str(response.get("turn_id", "")).strip()
    else:
        message = str(response).strip()
        turn_id = ""
    if not message:
        raise ValueError("resume input must contain a non-empty message")
    if not turn_id:
        turn_id = stable_contract_id("turn", {"message": message})
    return message, turn_id


def _append_recent_turn(
    turns: list[dict[str, str]], message: str, max_turns: int
) -> list[dict[str, str]]:
    return [*turns, {"role": "user", "content": message}][-max_turns:]


def _is_budget_or_failure_pause(state: AgentGraphState) -> bool:
    reason = str(state.get("pause_reason", ""))
    return reason.endswith("_exceeded") or reason.endswith("_invalid") or reason == "turn_timeout"
