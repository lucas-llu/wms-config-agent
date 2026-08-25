"""Stable, JSON-safe contracts for V2 configuration-agent workflows."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, TypedDict, cast

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class AgentContractError(ValueError):
    """Raised when an agent contract violates a stable boundary."""


class IntentType(StrEnum):
    ATOMIC_QUERY = "atomic_query"
    CONFIGURE_GOAL = "configure_goal"
    INSPECT_DRAFT = "inspect_draft"
    UNSUPPORTED = "unsupported"


class AgentRole(StrEnum):
    SYSTEM = "system"
    SUPERVISOR = "supervisor"
    REQUIREMENT = "requirement"
    PLANNING = "planning"
    KNOWLEDGE = "knowledge"
    CONFLICT = "conflict"
    VALIDATION = "validation"
    COMPOSER = "composer"


class SessionStatus(StrEnum):
    CREATED = "created"
    COLLECTING_REQUIREMENTS = "collecting_requirements"
    PLANNING = "planning"
    RETRIEVING = "retrieving"
    VALIDATING = "validating"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    FAILED = "failed"


class TaskStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    BLOCKED = "blocked"
    VALIDATED = "validated"
    INVALIDATED = "invalidated"


class EvidenceStatus(StrEnum):
    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"
    SUPPORTED = "supported"
    ENVIRONMENT_VERIFIED = "environment_verified"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKING = "blocking"


class ReviewDecision(StrEnum):
    REVISE = "revise"
    REJECT = "reject"
    APPROVE = "approve"


@dataclass(frozen=True, slots=True)
class SerializableAgentContract:
    """Provide deterministic JSON serialization and content fingerprints."""

    def to_dict(self) -> dict[str, Any]:
        value = _json_compatible(asdict(self))
        return cast(dict[str, Any], value)

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ConfirmedContext(SerializableAgentContract):
    product_version: str | None = None
    modules: tuple[str, ...] = ()
    site: str | None = None
    environment: str | None = None
    business_process: str | None = None
    volume_profile: str | None = None
    integrations: tuple[str, ...] = ()
    customizations: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    risk_tolerance: RiskLevel = RiskLevel.MEDIUM

    def __post_init__(self) -> None:
        _validate_optional_text(self.product_version, "ConfirmedContext.product_version")
        _validate_optional_text(self.site, "ConfirmedContext.site")
        _validate_optional_text(self.environment, "ConfirmedContext.environment")
        _validate_optional_text(self.business_process, "ConfirmedContext.business_process")
        _validate_optional_text(self.volume_profile, "ConfirmedContext.volume_profile")
        _validate_non_empty_items(self.modules, "ConfirmedContext.modules")
        _validate_non_empty_items(self.integrations, "ConfirmedContext.integrations")
        _validate_non_empty_items(self.customizations, "ConfirmedContext.customizations")
        _validate_non_empty_items(self.constraints, "ConfirmedContext.constraints")


@dataclass(frozen=True, slots=True)
class Assumption(SerializableAgentContract):
    assumption_id: str
    text: str
    source_turn_id: str
    confirmed: bool = False

    def __post_init__(self) -> None:
        _validate_identifier(self.assumption_id, "Assumption.assumption_id")
        _validate_text(self.text, "Assumption.text")
        _validate_identifier(self.source_turn_id, "Assumption.source_turn_id")


@dataclass(frozen=True, slots=True)
class OpenQuestion(SerializableAgentContract):
    question_id: str
    text: str
    reason: str
    blocking: bool = True
    answered: bool = False

    def __post_init__(self) -> None:
        _validate_identifier(self.question_id, "OpenQuestion.question_id")
        _validate_text(self.text, "OpenQuestion.text")
        _validate_text(self.reason, "OpenQuestion.reason")


@dataclass(frozen=True, slots=True)
class Decision(SerializableAgentContract):
    decision_id: str
    summary: str
    rationale: str
    source_turn_id: str

    def __post_init__(self) -> None:
        _validate_identifier(self.decision_id, "Decision.decision_id")
        _validate_text(self.summary, "Decision.summary")
        _validate_text(self.rationale, "Decision.rationale")
        _validate_identifier(self.source_turn_id, "Decision.source_turn_id")


@dataclass(frozen=True, slots=True)
class ConfigurationParameter(SerializableAgentContract):
    name: str
    value: str | int | float | bool | None
    description: str
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.name, "ConfigurationParameter.name")
        _validate_text(self.description, "ConfigurationParameter.description")
        if self.value is not None and not isinstance(self.value, str | int | float | bool):
            raise AgentContractError("ConfigurationParameter.value must be a JSON scalar")
        if self.evidence_id is not None:
            _validate_identifier(self.evidence_id, "ConfigurationParameter.evidence_id")


@dataclass(frozen=True, slots=True)
class ConfigurationTask(SerializableAgentContract):
    task_id: str
    title: str
    module: str
    goal: str
    status: TaskStatus = TaskStatus.DRAFT
    depends_on: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    parameters: tuple[ConfigurationParameter, ...] = ()
    steps: tuple[str, ...] = ()
    validation_steps: tuple[str, ...] = ()
    rollback_steps: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    assumption_ids: tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.MEDIUM
    open_question_ids: tuple[str, ...] = ()
    evidence_status: EvidenceStatus = EvidenceStatus.UNSUPPORTED

    def __post_init__(self) -> None:
        _validate_identifier(self.task_id, "ConfigurationTask.task_id")
        _validate_text(self.title, "ConfigurationTask.title")
        _validate_identifier(self.module, "ConfigurationTask.module")
        _validate_text(self.goal, "ConfigurationTask.goal")
        _validate_identifiers(self.depends_on, "ConfigurationTask.depends_on")
        _validate_non_empty_items(self.preconditions, "ConfigurationTask.preconditions")
        _validate_non_empty_items(self.steps, "ConfigurationTask.steps")
        _validate_non_empty_items(self.validation_steps, "ConfigurationTask.validation_steps")
        _validate_non_empty_items(self.rollback_steps, "ConfigurationTask.rollback_steps")
        _validate_identifiers(self.evidence_ids, "ConfigurationTask.evidence_ids")
        _validate_identifiers(self.assumption_ids, "ConfigurationTask.assumption_ids")
        _validate_identifiers(self.open_question_ids, "ConfigurationTask.open_question_ids")
        if self.task_id in self.depends_on:
            raise AgentContractError("ConfigurationTask cannot depend on itself")


@dataclass(frozen=True, slots=True)
class Evidence(SerializableAgentContract):
    evidence_id: str
    chunk_id: str
    source: str
    excerpt: str
    score: float
    page_start: int | None = None
    page_end: int | None = None
    product_version: str | None = None
    module: str | None = None
    collection: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.evidence_id, "Evidence.evidence_id")
        _validate_identifier(self.chunk_id, "Evidence.chunk_id")
        _validate_text(self.source, "Evidence.source")
        _validate_text(self.excerpt, "Evidence.excerpt")
        if isinstance(self.score, bool) or not isinstance(self.score, int | float):
            raise AgentContractError("Evidence.score must be a number")
        if self.page_start is not None and self.page_start < 1:
            raise AgentContractError("Evidence.page_start must be greater than 0")
        if self.page_end is not None and self.page_end < 1:
            raise AgentContractError("Evidence.page_end must be greater than 0")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise AgentContractError("Evidence.page_end must not precede page_start")
        _validate_optional_text(self.product_version, "Evidence.product_version")
        _validate_optional_text(self.module, "Evidence.module")
        _validate_optional_text(self.collection, "Evidence.collection")


@dataclass(frozen=True, slots=True)
class ConfigurationConflict(SerializableAgentContract):
    conflict_id: str
    summary: str
    dimension: str
    task_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    blocking: bool = True

    def __post_init__(self) -> None:
        _validate_identifier(self.conflict_id, "ConfigurationConflict.conflict_id")
        _validate_text(self.summary, "ConfigurationConflict.summary")
        _validate_identifier(self.dimension, "ConfigurationConflict.dimension")
        _validate_identifiers(self.task_ids, "ConfigurationConflict.task_ids", require_items=True)
        _validate_identifiers(
            self.evidence_ids, "ConfigurationConflict.evidence_ids", require_items=True
        )


@dataclass(frozen=True, slots=True)
class ValidationFinding(SerializableAgentContract):
    finding_id: str
    rule_id: str
    message: str
    severity: FindingSeverity
    task_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.finding_id, "ValidationFinding.finding_id")
        _validate_identifier(self.rule_id, "ValidationFinding.rule_id")
        _validate_text(self.message, "ValidationFinding.message")
        _validate_identifiers(self.task_ids, "ValidationFinding.task_ids")
        _validate_identifiers(self.evidence_ids, "ValidationFinding.evidence_ids")


@dataclass(frozen=True, slots=True)
class ExportArtifact(SerializableAgentContract):
    format: str
    path: str
    fingerprint: str

    def __post_init__(self) -> None:
        _validate_identifier(self.format, "ExportArtifact.format")
        _validate_text(self.path, "ExportArtifact.path")
        if not re.fullmatch(r"[0-9a-f]{64}", self.fingerprint):
            raise AgentContractError("ExportArtifact.fingerprint must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class ConfigurationSolution(SerializableAgentContract):
    session_id: str
    revision: int
    goal: str
    context: ConfirmedContext
    tasks: tuple[ConfigurationTask, ...]
    evidence: tuple[Evidence, ...]
    generated_at: str
    knowledge_fingerprint: str
    prompt_version: str
    assumptions: tuple[Assumption, ...] = ()
    decisions: tuple[Decision, ...] = ()
    conflicts: tuple[ConfigurationConflict, ...] = ()
    findings: tuple[ValidationFinding, ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.session_id, "ConfigurationSolution.session_id")
        _validate_revision(self.revision)
        _validate_text(self.goal, "ConfigurationSolution.goal")
        _validate_unique_contract_ids(self.tasks, "task_id", "ConfigurationSolution.tasks")
        _validate_unique_contract_ids(
            self.evidence, "evidence_id", "ConfigurationSolution.evidence"
        )
        _validate_text(self.generated_at, "ConfigurationSolution.generated_at")
        _validate_text(
            self.knowledge_fingerprint,
            "ConfigurationSolution.knowledge_fingerprint",
        )
        _validate_text(self.prompt_version, "ConfigurationSolution.prompt_version")


class ConfigurationSessionState(TypedDict, total=False):
    """Checkpoint state. Values must remain JSON/msgpack serializable."""

    session_id: str
    revision: int
    status: str
    created_at: str
    updated_at: str
    user_goal: str
    intent: str
    active_agent: str
    next_action: str
    confirmed_context: dict[str, Any]
    assumptions: list[dict[str, Any]]
    open_questions: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    configuration_tasks: list[dict[str, Any]]
    dependency_edges: list[dict[str, Any]]
    evidence_registry: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    validation_findings: list[dict[str, Any]]
    draft_version: int
    review_decision: str
    export_artifacts: list[dict[str, Any]]
    nodes_executed: int
    tool_calls_made: int
    retry_count: int
    trace_id: str


SYSTEM_MANAGED_FIELDS = frozenset(
    {
        "session_id",
        "revision",
        "created_at",
        "updated_at",
        "trace_id",
    }
)

STATE_FIELD_OWNERS = MappingProxyType(
    {
        AgentRole.SYSTEM: SYSTEM_MANAGED_FIELDS,
        AgentRole.SUPERVISOR: frozenset(
            {
                "status",
                "intent",
                "active_agent",
                "next_action",
                "nodes_executed",
                "tool_calls_made",
                "retry_count",
            }
        ),
        AgentRole.REQUIREMENT: frozenset(
            {"user_goal", "confirmed_context", "assumptions", "open_questions", "decisions"}
        ),
        AgentRole.PLANNING: frozenset({"configuration_tasks", "dependency_edges"}),
        AgentRole.KNOWLEDGE: frozenset({"evidence_registry"}),
        AgentRole.CONFLICT: frozenset({"conflicts"}),
        AgentRole.VALIDATION: frozenset({"validation_findings"}),
        AgentRole.COMPOSER: frozenset({"draft_version", "review_decision", "export_artifacts"}),
    }
)


def validate_state_update(role: AgentRole, update: dict[str, Any]) -> None:
    """Reject nodes that attempt to mutate state owned by another role."""

    allowed = STATE_FIELD_OWNERS[role]
    illegal = sorted(set(update) - allowed)
    if illegal:
        fields = ", ".join(illegal)
        raise AgentContractError(f"Agent role {role.value!r} cannot update fields: {fields}")
    _json_compatible(update)


def stable_contract_id(prefix: str, payload: Any, *, digest_length: int = 16) -> str:
    """Create a stable identifier from canonical JSON content."""

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", prefix):
        raise AgentContractError("stable ID prefix must be lowercase snake_case")
    if not 8 <= digest_length <= 64:
        raise AgentContractError("stable ID digest_length must be between 8 and 64")
    if isinstance(payload, SerializableAgentContract):
        payload = payload.to_dict()
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:digest_length]}"


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value with deterministic ordering."""

    compatible = _json_compatible(value)
    try:
        return json.dumps(
            compatible,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except ValueError as exc:
        raise AgentContractError(f"agent contract is not strict JSON: {exc}") from exc


def _json_compatible(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AgentContractError("agent contract dictionaries must use string keys")
            result[key] = _json_compatible(item)
        return result
    if isinstance(value, list | tuple):
        return [_json_compatible(item) for item in value]
    raise AgentContractError(
        f"agent contract value of type {type(value).__name__} is not JSON serializable"
    )


def _validate_identifier(value: str, field_path: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AgentContractError(
            f"{field_path} must be a non-empty stable identifier using letters, digits, ._:-"
        )


def _validate_identifiers(
    values: tuple[str, ...], field_path: str, *, require_items: bool = False
) -> None:
    if require_items and not values:
        raise AgentContractError(f"{field_path} must not be empty")
    for value in values:
        _validate_identifier(value, field_path)
    if len(values) != len(set(values)):
        raise AgentContractError(f"{field_path} must not contain duplicates")


def _validate_text(value: str, field_path: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AgentContractError(f"{field_path} must be a non-empty string")


def _validate_optional_text(value: str | None, field_path: str) -> None:
    if value is not None:
        _validate_text(value, field_path)


def _validate_non_empty_items(values: tuple[str, ...], field_path: str) -> None:
    for value in values:
        _validate_text(value, field_path)


def _validate_revision(revision: int) -> None:
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise AgentContractError("revision must be an integer greater than 0")


def _validate_unique_contract_ids(
    contracts: tuple[SerializableAgentContract, ...], attribute: str, field_path: str
) -> None:
    values = [getattr(contract, attribute) for contract in contracts]
    if len(values) != len(set(values)):
        raise AgentContractError(f"{field_path} must not contain duplicate {attribute} values")
