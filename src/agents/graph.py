"""Explicit LangGraph supervisor workflow for intent and requirement collection."""

from __future__ import annotations

from types import MappingProxyType
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agents.budget import TurnBudgetPolicy
from agents.contracts import IntentType, OpenQuestion, SessionStatus, stable_contract_id
from agents.conversation_memory import ContextLimitError, prepare_memory, resolve_followup
from agents.language import localized, response_language
from agents.llm_json import StructuredLLMError, invoke_json
from agents.nodes import (
    IntentClassifier,
    KnowledgeAgent,
    KnowledgeCollectionError,
    PlanningAgent,
    RequirementAgent,
)
from agents.services import ValidationService
from agents.services.answer_pipeline import AnswerBudgetExceeded, AnswerPipeline
from agents.services.evidence_decision import EvidenceDecisionService
from agents.tools.knowledge_adapter import build_scope_filters
from agents.workspace import Workspace, WorkspaceScopeError
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
    workspace_id: str
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
    memory_history: list[dict[str, Any]]
    conversation_summary: str
    memory_through_sequence: int
    conversation_recent: list[dict[str, str]]
    conversation_context: str
    resolved_user_message: str
    requirement_summary: str
    confirmed_context: Annotated[dict[str, Any], merge_context]
    assumptions: Annotated[list[dict[str, Any]], merge_assumptions]
    open_questions: list[dict[str, Any]]
    configuration_tasks: list[dict[str, Any]]
    dependency_edges: list[dict[str, Any]]
    planning_baseline_fingerprint: str
    invalidated_task_ids: list[str]
    evidence_registry: list[dict[str, Any]]
    task_evidence_bindings: list[dict[str, Any]]
    knowledge_fingerprint: str
    conflicts: list[dict[str, Any]]
    validation_findings: list[dict[str, Any]]
    targeted_retrieval_requirements: dict[str, list[str]]
    targeted_retrieval_rounds: int
    validation_fingerprint: str
    nodes_executed: int
    retry_count: int
    tokens_used: int
    tool_calls_made: int
    turn_deadline_epoch: float
    trace_id: str
    assistant_reply: str
    answer_evidence: list[dict[str, Any]]
    answer_status: str
    response_language: str
    evidence_decision_report: dict[str, Any]
    answer_strategy: str
    answer_recovery: str


ALLOWED_TRANSITIONS = MappingProxyType(
    {
        SessionStatus.CREATED: frozenset(
            {SessionStatus.COLLECTING_REQUIREMENTS, SessionStatus.PAUSED, SessionStatus.FAILED}
        ),
        SessionStatus.COLLECTING_REQUIREMENTS: frozenset(
            {
                SessionStatus.COLLECTING_REQUIREMENTS,
                SessionStatus.VALIDATING,
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
        SessionStatus.PLANNING: frozenset(
            {SessionStatus.RETRIEVING, SessionStatus.PAUSED, SessionStatus.FAILED}
        ),
        SessionStatus.RETRIEVING: frozenset(
            {SessionStatus.VALIDATING, SessionStatus.PAUSED, SessionStatus.FAILED}
        ),
        SessionStatus.VALIDATING: frozenset(
            {
                SessionStatus.VALIDATING,
                SessionStatus.REVIEW_REQUIRED,
                SessionStatus.PAUSED,
                SessionStatus.FAILED,
            }
        ),
    }
)

INTENT_ACTIONS = MappingProxyType(
    {
        IntentType.ATOMIC_QUERY: "query_knowledge",
        IntentType.CONFIGURE_GOAL: "extract_requirements",
        IntentType.INSPECT_DRAFT: "render_current_draft",
        IntentType.UNSUPPORTED: "bounded_rejection",
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
        planning_agent: PlanningAgent,
        knowledge_agent: KnowledgeAgent | None,
        validation_service: ValidationService,
        budget: TurnBudgetPolicy,
        workspace: Workspace | None = None,
        evidence_decision_service: EvidenceDecisionService | None = None,
    ) -> None:
        self.settings = settings
        self.classifier = classifier
        self.requirement_agent = requirement_agent
        self.planning_agent = planning_agent
        self.knowledge_agent = knowledge_agent
        self.validation_service = validation_service
        self.budget = budget
        self.workspace = workspace
        self.evidence_decision_service = evidence_decision_service

    def compile(self, checkpointer: BaseCheckpointSaver[Any]) -> Any:
        builder = StateGraph(AgentGraphState)
        builder.add_node("prepare_memory", self._prepare_memory)
        builder.add_node("classify_intent", self._classify_intent)
        builder.add_node("answer_question", self._answer_question)
        builder.add_node("await_question", self._await_question)
        builder.add_node("pause_intent", self._pause_intent)
        builder.add_node("await_intent", self._await_intent)
        builder.add_node("extract_requirements", self._extract_requirements)
        builder.add_node("pause_requirements", self._pause_requirements)
        builder.add_node("await_requirements", self._await_requirements)
        builder.add_node("complete_requirements", self._complete_requirements)
        builder.add_node("plan_tasks", self._plan_tasks)
        if self.knowledge_agent is not None:
            builder.add_node("retrieve_evidence", self._retrieve_evidence)
            builder.add_node("analyze_conflicts", self._analyze_conflicts)
            builder.add_node("validate_draft", self._validate_draft)
            builder.add_node("targeted_retrieval", self._targeted_retrieval)
            builder.add_node("pause_validation", self._pause_validation)
            builder.add_node("await_validation", self._await_validation)
            builder.add_node("complete_validation", self._complete_validation)
        builder.add_edge(START, "prepare_memory")
        builder.add_conditional_edges(
            "prepare_memory",
            self._route_after_memory,
            {"classify": "classify_intent", "requirements": "extract_requirements", "end": END},
        )
        builder.add_conditional_edges(
            "classify_intent",
            self._route_after_classification,
            {
                "clarify": "pause_intent",
                "configure": "extract_requirements",
                "answer": "answer_question",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "answer_question",
            self._route_after_await,
            {"continue": "await_question", "end": END},
        )
        builder.add_conditional_edges(
            "await_question",
            self._route_after_await,
            {"continue": "prepare_memory", "end": END},
        )
        builder.add_conditional_edges(
            "pause_intent", self._route_pause, {"await": "await_intent", "end": END}
        )
        builder.add_conditional_edges(
            "await_intent",
            self._route_after_await,
            {"continue": "prepare_memory", "end": END},
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
            {"continue": "prepare_memory", "end": END},
        )
        builder.add_edge("complete_requirements", "plan_tasks")
        if self.knowledge_agent is None:
            builder.add_edge("plan_tasks", END)
        else:
            builder.add_conditional_edges(
                "plan_tasks",
                self._route_after_await,
                {"continue": "retrieve_evidence", "end": END},
            )
            builder.add_conditional_edges(
                "retrieve_evidence",
                self._route_after_await,
                {"continue": "analyze_conflicts", "end": END},
            )
            builder.add_edge("analyze_conflicts", "validate_draft")
            builder.add_conditional_edges(
                "validate_draft",
                self._route_after_validation,
                {
                    "retry": "targeted_retrieval",
                    "pause": "pause_validation",
                    "review": "complete_validation",
                },
            )
            builder.add_edge("targeted_retrieval", "analyze_conflicts")
            builder.add_edge("pause_validation", "await_validation")
            builder.add_conditional_edges(
                "await_validation",
                self._route_after_await,
                {"continue": "prepare_memory", "end": END},
            )
            builder.add_edge("complete_validation", END)
        return builder.compile(checkpointer=checkpointer, name="configuration-supervisor")

    def _prepare_memory(self, state: AgentGraphState) -> dict[str, Any]:
        update: dict[str, Any] = {"resolved_user_message": "", "memory_history": []}

        def invoke(prompt, validator):
            current = {**state, **update}
            entered = self.budget.enter_node(current, "memory")
            update.update(entered.update)
            if not entered.allowed:
                raise ContextLimitError("Turn budget exhausted")
            # Conservative byte-based input estimate; actual provider usage is
            # accounted below. This is independent of the model context capacity.
            remaining = self.settings.max_tokens_per_turn - int(current.get("tokens_used", 0))
            if len(prompt.encode("utf-8")) + 1024 > remaining:
                update.update(self.budget.pause_update(current, "token_budget_exceeded", "memory"))
                raise ContextLimitError("Insufficient turn budget for memory request")
            try:
                result = invoke_json(
                    self.classifier.llm,
                    [{"role": "user", "content": prompt}],
                    max_retries=0,
                    validator=validator,
                )
            except StructuredLLMError as exc:
                update.update(
                    self.budget.account_llm(
                        current,
                        retries=exc.retries,
                        tokens_used=exc.tokens_used,
                        node_name="memory",
                    ).update
                )
                raise
            accounted = self.budget.account_llm(
                current, retries=result.retries, tokens_used=result.tokens_used, node_name="memory"
            )
            update.update(accounted.update)
            if not accounted.allowed:
                raise ContextLimitError("Turn budget exhausted")
            return result.payload

        try:
            update.update(
                prepare_memory(
                    state.get("memory_history", []),
                    summary=state.get("conversation_summary", ""),
                    through=state.get("memory_through_sequence", 0),
                    confirmed=dict(state.get("confirmed_context", {})),
                    settings=self.settings,
                    invoke=invoke,
                )
            )
            if state.get("next_action") != "extract_requirements" and (
                len(update.get("conversation_recent", [])) > 1 or update.get("conversation_summary")
            ):
                resolved = resolve_followup(
                    state["latest_user_message"],
                    update["conversation_context"],
                    invoke,
                    language=_language(state),
                )
                if resolved["clarification"]:
                    return {
                        **update,
                        "status": "paused",
                        "pause_reason": "context_reference_invalid",
                        "assistant_reply": resolved["clarification"],
                        "answer_evidence": [],
                        "answer_status": "context_paused",
                        "next_action": "clarify_reference",
                    }
                update["resolved_user_message"] = resolved["question"].strip()
            return update
        except (ContextLimitError, StructuredLLMError) as exc:
            reason = update.get("pause_reason") or (
                "memory_output_invalid"
                if isinstance(exc, StructuredLLMError)
                else "context_limit_exceeded"
            )
            if reason == "memory_output_invalid":
                reply = localized(
                    _language(state),
                    "本轮历史摘要或追问整理失败，原始对话已保留。请稍后重试，"
                    "或直接说明涉及的配置及本次问题。",
                    "History summarization or follow-up resolution failed. Original messages "
                    "are saved. Retry later or specify the configuration and current question.",
                )
            elif reason == "context_limit_exceeded":
                reply = localized(
                    _language(state),
                    "本轮内容超过对话上下文上限，无法在保留必要内容的情况下完成整理。"
                    "原始对话已保留。请缩短过长输入；若历史或已确认需求过多，"
                    "请新建对话并提供本次所需背景。",
                    "This conversation exceeds the context limit and cannot be compacted "
                    "without losing required content. Original messages are saved. Shorten "
                    "oversized input; for excessive history or requirements, start a new "
                    "conversation with the relevant background.",
                )
            else:
                reply = localized(
                    _language(state),
                    "整理对话上下文时达到本轮处理预算或时间上限，已暂停处理并保留原始对话。"
                    "请稍后重试，或缩小本次问题范围。",
                    "Context preparation reached the turn budget or time limit. Processing "
                    "paused and original messages are saved. Retry later or narrow the request.",
                )
            return {
                **update,
                "status": "paused",
                "pause_reason": reason,
                "assistant_reply": reply,
                "answer_evidence": [],
                "answer_status": "context_paused",
                "next_action": "clarify_context",
            }

    @staticmethod
    def _route_after_memory(state: AgentGraphState) -> str:
        if _is_budget_or_failure_pause(state):
            return "end"
        return "requirements" if state.get("next_action") == "extract_requirements" else "classify"

    def _classify_intent(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "supervisor")
        if not entered.allowed:
            return entered.update
        try:
            result = self.classifier.classify(
                state.get("resolved_user_message") or state["latest_user_message"]
            )
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
                "next_action": INTENT_ACTIONS[result.intent],
                "pause_reason": "",
                "assistant_reply": "",
                "answer_evidence": [],
                "answer_status": "",
                "response_language": _language(state),
            }
        )
        return update

    def _answer_question(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "knowledge")
        if self.evidence_decision_service is not None:
            entered.update["evidence_decision_report"] = {}
        if not entered.allowed:
            return entered.update
        language = _language(state)
        reply = localized(
            language,
            "我可以查询 WMS 配置文档或协助制定配置方案，请描述相关问题。",
            "I can look up WMS configuration documents or help plan a configuration. "
            "Please describe a related question.",
        )
        calls = 0
        answer_evidence = []
        answer_status = "no_evidence"
        decision_report = {}
        pipeline = None
        if state.get("intent") == IntentType.INSPECT_DRAFT.value:
            tasks = state.get("configuration_tasks", [])
            reply = (
                localized(
                    language,
                    "当前还没有配置草稿。请先描述需要配置的流程。",
                    "There is no configuration draft yet. Describe the workflow first.",
                )
                if not tasks
                else (
                    localized(language, "当前草稿任务：\n", "Current draft tasks:\n")
                    + "\n".join(str(item.get("title", "")) for item in tasks)
                    + localized(
                        language,
                        "\n请在工作区查看详情；草稿不代表已批准。",
                        "\nView the details in Workspace. The draft is not approved.",
                    )
                )
            )
        elif state.get("intent") == IntentType.ATOMIC_QUERY.value:
            reply = localized(
                language,
                "未找到足够的文档证据。请补充模块、版本或流程编码，或先导入相关文档。",
                "There is not enough document evidence. Specify the module, version or "
                "process code, or import the relevant documents.",
            )
            if self.knowledge_agent is not None:
                try:
                    confirmed = state.get("confirmed_context", {})
                    modules = confirmed.get("modules", [])
                    module = modules[0] if len(modules) == 1 else ""
                    filters = build_scope_filters(confirmed, module=module)
                    if self.workspace:
                        filters = self.workspace.filters(filters)
                    pipeline = AnswerPipeline(
                        self.classifier.llm,
                        self.knowledge_agent.adapter,
                        check_budget=lambda tokens, retries: (
                            self.budget.account_llm(
                                state, retries=retries, tokens_used=tokens, node_name="knowledge"
                            ).allowed
                        ),
                    )
                    try:
                        answer = pipeline.run(
                            state.get("resolved_user_message") or state["latest_user_message"],
                            filters=filters,
                            language=language,
                            conversation=state.get("conversation_context", ""),
                            shadow_service=self.evidence_decision_service,
                            context=state.get("confirmed_context", {}),
                            strategy=state.get("answer_strategy", "standard"),
                        )
                    finally:
                        calls = pipeline.searches
                        decision_report = pipeline.report
                        accounted = self.budget.account_llm(
                            state,
                            retries=pipeline.retries,
                            tokens_used=pipeline.tokens,
                            node_name="knowledge",
                        )
                        entered.update.update(accounted.update)
                    reply = answer.text
                    answer_status = answer.status
                    answer_evidence = [
                        {
                            **item.to_dict(),
                            "citation_index": index,
                            "supporting_quotes": [
                                quote
                                for source_id, quote in answer.supporting_quotes
                                if source_id == index
                            ],
                        }
                        for index, item in enumerate(pipeline.evidence, 1)
                        if index in answer.cited_source_ids
                    ]
                except AnswerBudgetExceeded:
                    answer_status = "budget_exceeded"
                    reply = localized(
                        language,
                        "本次回答达到回合预算限制，请稍后重试。",
                        "The answer reached this turn's budget limit. Retry later.",
                    )
                except WorkspaceScopeError:
                    reply = localized(
                        language,
                        "当前 Workspace 需要更明确的查询范围，请联系管理员选择范围。",
                        "This workspace requires a more specific query scope. "
                        "Contact the administrator.",
                    )
                except StructuredLLMError as exc:
                    answer_status = "generation_failed"
                    accounted = self.budget.account_llm(
                        state,
                        retries=pipeline.retries if pipeline else exc.retries,
                        tokens_used=pipeline.tokens if pipeline else exc.tokens_used,
                        node_name="knowledge",
                    )
                    if not accounted.allowed:
                        return {
                            **entered.update,
                            **accounted.update,
                            "assistant_reply": localized(
                                language,
                                "回答生成达到预算限制，未输出未经校验的结论。",
                                "Answer generation reached the budget limit; "
                                "no unvalidated conclusion was returned.",
                            ),
                        }
                    entered.update.update(accounted.update)
                    reply = localized(
                        language,
                        "找到了相关文档，但未能生成通过引用和语言校验的答案；请补充问题或稍后重试。",
                        "Relevant documents were found, but the answer failed citation or language "
                        "validation. Clarify the question or retry later.",
                    )
                except Exception:
                    answer_status = "retrieval_failed"
                    reply = localized(
                        language,
                        "知识库查询暂时失败，请稍后重试或检查索引；本次没有生成配置结论。",
                        "Knowledge retrieval failed. Retry later or check the index; "
                        "no configuration "
                        "conclusion was generated.",
                    )
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.PAUSED),
            "pause_reason": entered.update.get("pause_reason", "question_answered"),
            "assistant_reply": reply,
            "answer_evidence": answer_evidence,
            "answer_status": answer_status,
            "open_questions": [],
            "tool_calls_made": int(state.get("tool_calls_made", 0)) + calls,
            "evidence_decision_report": decision_report,
            "answer_recovery": pipeline.recovery if pipeline else "not_needed",
        }

    def _await_question(self, state: AgentGraphState) -> dict[str, Any]:
        response = interrupt({"kind": "question_answered"})
        message, turn_id = _resume_message(response)
        return {
            "status": transition_status(state, SessionStatus.CREATED),
            "latest_user_message": message,
            "latest_turn_id": turn_id,
            "response_language": response_language(message, _language(state)),
            "recent_turns": _append_recent_turn(
                state.get("recent_turns", []), message, self.settings.max_context_turns
            ),
            "assistant_reply": "",
            "answer_evidence": [],
            "answer_status": "",
            "next_action": "classify_intent",
            "pause_reason": "",
            "open_questions": [],
        }

    def _pause_intent(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "supervisor")
        if not entered.allowed:
            return entered.update
        question = OpenQuestion(
            question_id=stable_contract_id("question", {"field": "intent"}),
            text=localized(
                _language(state),
                "你希望查询一个配置问题，还是制定完整的配置方案？",
                "Are you asking a one-time question or building a complete configuration plan?",
            ),
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
            "response_language": response_language(message, _language(state)),
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
                recent_turns=list(state.get("conversation_recent", state.get("recent_turns", []))),
                requirement_summary=str(state.get("requirement_summary", "")),
                conversation_summary=state.get("conversation_summary", ""),
                language=_language(state),
            )
        except StructuredLLMError as exc:
            return self._structured_failure(
                state, entered.update, exc, "requirement_output_invalid", "requirement"
            )
        if self.workspace is not None:
            try:
                self.workspace.validate_state({"confirmed_context": result.confirmed_context})
            except WorkspaceScopeError:
                return {
                    **entered.update,
                    "status": "paused",
                    "pause_reason": "workspace_scope_invalid",
                    "next_action": "provide_allowed_scope",
                }
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
            "response_language": response_language(message, _language(state)),
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

    def _plan_tasks(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "planning")
        if not entered.allowed:
            return entered.update
        try:
            result = self.planning_agent.plan(
                user_goal=state["user_goal"],
                confirmed_context=dict(state.get("confirmed_context", {})),
                assumptions=list(state.get("assumptions", [])),
                previous_tasks=list(state.get("configuration_tasks", [])),
                language=_language(state),
            )
        except StructuredLLMError as exc:
            return self._structured_failure(
                state, entered.update, exc, "planning_output_invalid", "planning"
            )
        accounted = self.budget.account_llm(
            state,
            retries=result.retries,
            tokens_used=result.tokens_used,
            node_name="planning",
        )
        update = {**entered.update, **accounted.update}
        if not accounted.allowed:
            return update
        plan = result.plan
        if self.workspace is not None:
            try:
                self.workspace.validate_state(
                    {"configuration_tasks": [t.to_dict() for t in plan.tasks]}
                )
            except WorkspaceScopeError:
                return {
                    **update,
                    "status": "paused",
                    "pause_reason": "workspace_scope_invalid",
                    "next_action": "provide_allowed_scope",
                }
        update.update(
            {
                "status": transition_status(state, SessionStatus.RETRIEVING),
                "active_agent": "planning",
                "configuration_tasks": [task.to_dict() for task in plan.tasks],
                "dependency_edges": [edge.to_dict() for edge in plan.edges],
                "planning_baseline_fingerprint": plan.baseline_fingerprint,
                "invalidated_task_ids": list(plan.invalidated_task_ids),
                "next_action": "retrieve_evidence",
                "pause_reason": "",
            }
        )
        return update

    def _retrieve_evidence(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "knowledge")
        if not entered.allowed:
            return entered.update
        if self.knowledge_agent is None:
            raise RuntimeError("knowledge agent is not configured")
        try:
            result = self.knowledge_agent.collect(
                tasks=list(state.get("configuration_tasks", [])),
                confirmed_context=dict(state.get("confirmed_context", {})),
                existing_evidence=list(state.get("evidence_registry", [])),
            )
        except KnowledgeCollectionError:
            return {
                **entered.update,
                "status": SessionStatus.PAUSED.value,
                "active_agent": "knowledge",
                "pause_reason": "knowledge_contract_invalid",
                "next_action": "repair_task_plan",
            }
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.VALIDATING),
            "active_agent": "knowledge",
            "evidence_registry": [item.to_dict() for item in result.evidence],
            "task_evidence_bindings": [item.to_dict() for item in result.bindings],
            "knowledge_fingerprint": result.knowledge_fingerprint,
            "tool_calls_made": int(state.get("tool_calls_made", 0)) + result.tool_calls_made,
            "next_action": "analyze_dependencies_and_conflicts",
            "pause_reason": "",
        }

    def _analyze_conflicts(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "conflict")
        if not entered.allowed:
            return entered.update
        report = self._validation_report(state)
        return {
            **entered.update,
            "active_agent": "conflict",
            "conflicts": [item.to_dict() for item in report.conflicts],
            "next_action": "validate_draft",
        }

    def _validate_draft(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "validation")
        if not entered.allowed:
            return entered.update
        report = self._validation_report(state)
        return {
            **entered.update,
            "active_agent": "validation",
            "conflicts": [item.to_dict() for item in report.conflicts],
            "validation_findings": [item.to_dict() for item in report.findings],
            "targeted_retrieval_requirements": {
                key: list(value) for key, value in report.targeted_requirements.items()
            },
            "validation_fingerprint": report.fingerprint,
            "next_action": "evaluate_validation",
        }

    def _targeted_retrieval(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "knowledge")
        if not entered.allowed:
            return entered.update
        if self.knowledge_agent is None:
            raise RuntimeError("knowledge agent is not configured")
        result = self.knowledge_agent.collect_targeted(
            tasks=list(state.get("configuration_tasks", [])),
            confirmed_context=dict(state.get("confirmed_context", {})),
            existing_evidence=list(state.get("evidence_registry", [])),
            existing_bindings=list(state.get("task_evidence_bindings", [])),
            requirements=dict(state.get("targeted_retrieval_requirements", {})),
        )
        return {
            **entered.update,
            "status": SessionStatus.VALIDATING.value,
            "active_agent": "knowledge",
            "evidence_registry": [item.to_dict() for item in result.evidence],
            "task_evidence_bindings": [item.to_dict() for item in result.bindings],
            "knowledge_fingerprint": result.knowledge_fingerprint,
            "tool_calls_made": int(state.get("tool_calls_made", 0)) + result.tool_calls_made,
            "targeted_retrieval_rounds": int(state.get("targeted_retrieval_rounds", 0)) + 1,
            "next_action": "analyze_dependencies_and_conflicts",
        }

    def _pause_validation(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "supervisor")
        if not entered.allowed:
            return entered.update
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.PAUSED),
            "active_agent": "supervisor",
            "pause_reason": "validation_blocked",
            "next_action": "ask_user",
        }

    def _await_validation(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "supervisor")
        if not entered.allowed:
            return entered.update
        response = interrupt(
            {
                "kind": "validation_blocked",
                "conflicts": state.get("conflicts", []),
                "findings": state.get("validation_findings", []),
            }
        )
        message, turn_id = _resume_message(response)
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.COLLECTING_REQUIREMENTS),
            "latest_user_message": message,
            "latest_turn_id": turn_id,
            "response_language": response_language(message, _language(state)),
            "recent_turns": _append_recent_turn(
                state.get("recent_turns", []), message, self.settings.max_context_turns
            ),
            "assistant_reply": "",
            "open_questions": [],
            "pause_reason": "",
            "targeted_retrieval_rounds": 0,
            "next_action": "extract_requirements",
        }

    def _complete_validation(self, state: AgentGraphState) -> dict[str, Any]:
        entered = self.budget.enter_node(state, "supervisor")
        if not entered.allowed:
            return entered.update
        return {
            **entered.update,
            "status": transition_status(state, SessionStatus.REVIEW_REQUIRED),
            "active_agent": "supervisor",
            "next_action": "compose_draft",
            "pause_reason": "",
        }

    def _validation_report(self, state: AgentGraphState):
        return self.validation_service.validate(
            tasks=list(state.get("configuration_tasks", [])),
            dependency_edges=list(state.get("dependency_edges", [])),
            evidence_registry=list(state.get("evidence_registry", [])),
            bindings=list(state.get("task_evidence_bindings", [])),
            confirmed_context=dict(state.get("confirmed_context", {})),
            invalidated_task_ids=list(state.get("invalidated_task_ids", [])),
        )

    def _route_after_validation(self, state: AgentGraphState) -> str:
        blocking = bool(state.get("conflicts")) or any(
            str(item.get("severity")) == "blocking" for item in state.get("validation_findings", [])
        )
        if not blocking:
            return "review"
        retryable = bool(state.get("targeted_retrieval_requirements")) and not state.get(
            "conflicts"
        )
        if retryable and int(state.get("targeted_retrieval_rounds", 0)) < 2:
            return "retry"
        return "pause"

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
        return "answer"

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


def _language(state: AgentGraphState) -> str:
    return state.get("response_language") or response_language(
        state.get("latest_user_message", state.get("user_goal", ""))
    )


def _append_recent_turn(
    turns: list[dict[str, str]], message: str, max_turns: int
) -> list[dict[str, str]]:
    return [*turns, {"role": "user", "content": message}][-max_turns:]


def _is_budget_or_failure_pause(state: AgentGraphState) -> bool:
    reason = str(state.get("pause_reason", ""))
    return reason.endswith("_exceeded") or reason.endswith("_invalid") or reason == "turn_timeout"
