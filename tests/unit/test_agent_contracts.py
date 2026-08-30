from __future__ import annotations

import json

import pytest

from agents import (
    AgentContractError,
    AgentRole,
    ConfigurationConflict,
    ConfigurationParameter,
    ConfigurationSolution,
    ConfigurationTask,
    ConfirmedContext,
    DependencyEdge,
    Evidence,
    EvidenceStatus,
    FindingSeverity,
    RiskLevel,
    TaskStatus,
    ValidationFinding,
    canonical_json,
    stable_contract_id,
    state_fingerprint,
    validate_state_update,
)


def _evidence(evidence_id: str = "evidence:one") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        chunk_id="chunk:one",
        source="manuals/inbound.pdf",
        excerpt="Appointment capacity is configured for the receiving schedule.",
        score=0.91,
        page_start=12,
        page_end=13,
        product_version="2024.1",
        module="inbound",
    )


def _task(task_id: str = "task:appointment") -> ConfigurationTask:
    return ConfigurationTask(
        task_id=task_id,
        title="Configure inbound appointment capacity",
        module="inbound",
        goal="Control dock capacity by schedule",
        status=TaskStatus.READY,
        parameters=(
            ConfigurationParameter(
                name="capacity",
                value=10,
                description="Maximum appointments per schedule window",
                evidence_id="evidence:one",
            ),
        ),
        steps=("Open the appointment configuration.", "Set the capacity."),
        validation_steps=("Query the resulting capacity.",),
        rollback_steps=("Restore the recorded previous capacity.",),
        evidence_ids=("evidence:one",),
        risk_level=RiskLevel.MEDIUM,
        evidence_status=EvidenceStatus.SUPPORTED,
    )


def test_agent_contracts_serialize_enums_and_nested_dataclasses_deterministically() -> None:
    task = _task()

    payload = task.to_dict()

    assert payload["status"] == "ready"
    assert payload["evidence_status"] == "supported"
    assert payload["parameters"][0]["value"] == 10
    assert json.loads(task.to_json()) == payload
    assert task.fingerprint() == _task().fingerprint()


def test_planning_contracts_preserve_evidence_needs_and_dependency_direction() -> None:
    task = ConfigurationTask(
        task_id="task:capacity",
        title="Plan appointment capacity",
        module="appointment",
        goal="Define capacity behavior",
        evidence_requirements=("Version-matched capacity documentation",),
        baseline_fingerprint="baseline:one",
    )
    edge = DependencyEdge(
        upstream_task_id="task:scope",
        downstream_task_id=task.task_id,
        reason="scope must be confirmed first",
    )

    assert task.to_dict()["evidence_requirements"] == [
        "Version-matched capacity documentation"
    ]
    assert edge.to_dict()["downstream_task_id"] == task.task_id


def test_stable_ids_ignore_dictionary_insertion_order() -> None:
    first = stable_contract_id("task", {"module": "inbound", "goal": "appointment"})
    second = stable_contract_id("task", {"goal": "appointment", "module": "inbound"})

    assert first == second
    assert first.startswith("task:")


def test_state_fingerprint_is_stable_and_ignores_runtime_noise() -> None:
    first = {
        "session_id": "session:one",
        "revision": 1,
        "user_goal": "Configure inbound receiving",
        "updated_at": "2026-08-26T00:00:00Z",
        "trace_id": "trace:one",
        "nodes_executed": 2,
        "tokens_used": 120,
        "turn_deadline_epoch": 1000.0,
    }
    second = {
        "nodes_executed": 8,
        "tokens_used": 900,
        "turn_deadline_epoch": 2000.0,
        "trace_id": "trace:two",
        "updated_at": "2026-08-26T01:00:00Z",
        "user_goal": "Configure inbound receiving",
        "revision": 1,
        "session_id": "session:one",
    }

    assert state_fingerprint(first) == state_fingerprint(second)
    assert state_fingerprint(first, include_runtime_counters=True) != state_fingerprint(
        second, include_runtime_counters=True
    )
    second["revision"] = 2
    assert state_fingerprint(first) != state_fingerprint(second)


def test_canonical_json_rejects_non_serializable_checkpoint_values() -> None:
    with pytest.raises(AgentContractError, match="not JSON serializable"):
        canonical_json({"unsafe": object()})

    with pytest.raises(AgentContractError, match="not strict JSON"):
        canonical_json({"unsafe": float("nan")})


def test_configuration_parameter_rejects_non_scalar_values() -> None:
    with pytest.raises(AgentContractError, match="JSON scalar"):
        ConfigurationParameter(
            name="capacity",
            value=[10],  # type: ignore[arg-type]
            description="Capacity must remain a scalar value.",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "not-a-status"),
        ("risk_level", "unknown-risk"),
        ("evidence_status", "made-up"),
    ],
)
def test_configuration_task_rejects_invalid_enum_values(field: str, value: str) -> None:
    values = {
        "task_id": "task:invalid-enum",
        "title": "Reject invalid enum",
        "module": "inbound",
        "goal": "Keep checkpoint state valid",
        field: value,
    }

    with pytest.raises(AgentContractError, match=field):
        ConfigurationTask(**values)  # type: ignore[arg-type]


def test_other_contracts_reject_invalid_enum_values() -> None:
    with pytest.raises(AgentContractError, match="risk_tolerance"):
        ConfirmedContext(risk_tolerance="unknown")  # type: ignore[arg-type]

    with pytest.raises(AgentContractError, match="severity"):
        ValidationFinding(
            finding_id="finding:bad-severity",
            rule_id="invalid_enum",
            message="Invalid severity must not cross the contract boundary.",
            severity="urgent",  # type: ignore[arg-type]
        )


def test_field_ownership_rejects_cross_agent_mutation() -> None:
    validate_state_update(
        AgentRole.REQUIREMENT,
        {"user_goal": "Configure receiving", "open_questions": []},
    )

    with pytest.raises(AgentContractError, match="configuration_tasks"):
        validate_state_update(AgentRole.REQUIREMENT, {"configuration_tasks": []})


def test_day_three_fields_have_explicit_least_privilege_ownership() -> None:
    validate_state_update(
        AgentRole.SUPERVISOR,
        {
            "intent_confidence": 0.9,
            "intent_reason": "explicit configuration goal",
            "intent_needs_clarification": False,
            "pause_reason": "",
            "latest_user_message": "Configure receiving",
            "latest_turn_id": "turn:one",
            "recent_turns": [],
            "tokens_used": 12,
            "turn_deadline_epoch": 1000.0,
        },
    )
    validate_state_update(
        AgentRole.REQUIREMENT,
        {"requirement_summary": "Inbound receiving for DC01"},
    )

    with pytest.raises(AgentContractError, match="requirement_summary"):
        validate_state_update(AgentRole.SUPERVISOR, {"requirement_summary": "wrong owner"})
    with pytest.raises(AgentContractError, match="tokens_used"):
        validate_state_update(AgentRole.REQUIREMENT, {"tokens_used": 12})


def test_planning_fields_are_owned_only_by_planning_agent() -> None:
    update = {
        "configuration_tasks": [],
        "dependency_edges": [],
        "planning_baseline_fingerprint": "baseline:one",
        "invalidated_task_ids": [],
    }

    validate_state_update(AgentRole.PLANNING, update)

    with pytest.raises(AgentContractError, match="configuration_tasks"):
        validate_state_update(AgentRole.SUPERVISOR, update)


def test_configuration_task_rejects_self_dependency() -> None:
    with pytest.raises(AgentContractError, match="depend on itself"):
        ConfigurationTask(
            task_id="task:self",
            title="Invalid self dependency",
            module="inbound",
            goal="Demonstrate validation",
            depends_on=("task:self",),
        )


def test_frozen_contracts_reject_mutable_list_boundaries() -> None:
    with pytest.raises(AgentContractError, match="depends_on must be a tuple"):
        ConfigurationTask(
            task_id="task:mutable",
            title="Reject mutable boundaries",
            module="inbound",
            goal="Keep frozen contracts immutable",
            depends_on=["task:other"],  # type: ignore[arg-type]
        )


def test_evidence_rejects_invalid_page_range() -> None:
    with pytest.raises(AgentContractError, match="page_end"):
        Evidence(
            evidence_id="evidence:bad-page",
            chunk_id="chunk:bad-page",
            source="manual.pdf",
            excerpt="Configuration guidance.",
            score=0.5,
            page_start=4,
            page_end=3,
        )


def test_solution_rejects_duplicate_task_ids() -> None:
    with pytest.raises(AgentContractError, match="duplicate task_id"):
        ConfigurationSolution(
            session_id="session:one",
            revision=1,
            goal="Configure inbound receiving",
            context=ConfirmedContext(product_version="2024.1", modules=("inbound",)),
            tasks=(_task(), _task()),
            evidence=(_evidence(),),
            generated_at="2026-08-26T00:00:00Z",
            knowledge_fingerprint="knowledge-v1",
            prompt_version="agent-v1",
        )


def test_solution_rejects_invalid_nested_contract_types() -> None:
    with pytest.raises(AgentContractError, match="ConfirmedContext"):
        ConfigurationSolution(
            session_id="session:one",
            revision=1,
            goal="Configure inbound receiving",
            context={"module": "inbound"},  # type: ignore[arg-type]
            tasks=(_task(),),
            evidence=(_evidence(),),
            generated_at="2026-08-26T00:00:00Z",
            knowledge_fingerprint="knowledge-v1",
            prompt_version="agent-v1",
        )


def test_conflict_and_finding_require_stable_references() -> None:
    conflict = ConfigurationConflict(
        conflict_id="conflict:version",
        summary="Sources describe different product versions.",
        dimension="product_version",
        task_ids=("task:appointment",),
        evidence_ids=("evidence:one", "evidence:two"),
    )
    finding = ValidationFinding(
        finding_id="finding:coverage",
        rule_id="citation_coverage",
        message="One task has partial evidence.",
        severity=FindingSeverity.BLOCKING,
        task_ids=("task:appointment",),
    )

    assert conflict.to_dict()["blocking"] is True
    assert finding.to_dict()["severity"] == "blocking"
