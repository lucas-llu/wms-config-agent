from __future__ import annotations

import json

from agents import ReviewDecision
from agents.repositories import SessionRepository
from agents.services import SolutionService, ValidationService
from observability.evaluation import AgentEvaluationRunner, AgentGoldenDataset, AgentScenarioResult


def _review_state(session_id: str):
    return {
        "session_id": session_id,
        "status": "review_required",
        "user_goal": "Configure inbound appointments",
        "confirmed_context": {"product_version": "2024.1", "site": "DC01"},
        "configuration_tasks": [
            {
                "task_id": "task:one",
                "title": "Plan capacity",
                "module": "inbound",
                "goal": "Configure capacity",
                "depends_on": [],
                "preconditions": [],
                "steps": ["Configure capacity"],
                "validation_steps": ["Verify capacity"],
                "rollback_steps": ["Restore capacity"],
                "evidence_requirements": ["Capacity documentation"],
                "risk_level": "medium",
            }
        ],
        "dependency_edges": [],
        "evidence_registry": [
            {
                "evidence_id": "evidence:one",
                "chunk_id": "chunk:one",
                "source": "manual.pdf",
                "excerpt": "Supported capacity configuration",
                "score": 0.9,
                "product_version": "2024.1",
                "module": "inbound",
            }
        ],
        "task_evidence_bindings": [
            {
                "task_id": "task:one",
                "queries": [
                    {
                        "requirement": "Capacity documentation",
                        "query": "capacity configuration",
                        "filters": {"version": "2024.1"},
                    }
                ],
                "evidence_ids": ["evidence:one"],
                "evidence_status": "supported",
                "gap_reasons": [],
            }
        ],
        "conflicts": [],
        "validation_findings": [],
        "knowledge_fingerprint": "knowledge:one",
        "validation_fingerprint": "validation:one",
    }


def test_six_public_agent_golden_scenarios_form_a_release_candidate(tmp_path) -> None:
    database = tmp_path / "sessions.db"
    repository = SessionRepository(database)
    for session_id in ("session:normal", "session:other"):
        repository.create_session(session_id=session_id, goal="Configure inbound appointments")
        repository.update_revision(
            session_id=session_id,
            expected_revision=1,
            state_update=_review_state(session_id),
            actor="golden",
            reason="validated",
        )
    solutions = SolutionService(repository, tmp_path / "exports")
    approved = solutions.review(
        "session:normal",
        expected_revision=2,
        decision=ReviewDecision.APPROVE,
        actor="reviewer",
        comment="approved",
    )
    artifact = solutions.export("session:normal", expected_revision=3, format="json")
    revised = solutions.review(
        "session:other",
        expected_revision=2,
        decision=ReviewDecision.REVISE,
        actor="reviewer",
        comment="change site",
    )
    reopened = SessionRepository(database)
    assert reopened.get_session("session:normal").current_revision == 3
    assert reopened.get_session("session:other").current_revision == 3

    validator = ValidationService()
    base = _review_state("session:normal")
    gap_binding = {
        **base["task_evidence_bindings"][0],
        "evidence_ids": [],
        "evidence_status": "unsupported",
        "gap_reasons": ["insufficient_evidence"],
    }
    gap = validator.validate(
        tasks=base["configuration_tasks"],
        dependency_edges=[],
        evidence_registry=[],
        bindings=[gap_binding],
        confirmed_context=base["confirmed_context"],
        invalidated_task_ids=[],
    )
    conflict_evidence = [
        {
            **base["evidence_registry"][0],
            "evidence_id": "evidence:old",
            "product_version": "2023.1",
        },
        {
            **base["evidence_registry"][0],
            "evidence_id": "evidence:new",
            "product_version": "2025.1",
        },
    ]
    conflict_binding = {
        **base["task_evidence_bindings"][0],
        "evidence_ids": ["evidence:old", "evidence:new"],
    }
    conflict = validator.validate(
        tasks=base["configuration_tasks"],
        dependency_edges=[],
        evidence_registry=conflict_evidence,
        bindings=[conflict_binding],
        confirmed_context=base["confirmed_context"],
        invalidated_task_ids=[],
    )

    results = [
        AgentScenarioResult(
            scenario_id="normal-inbound",
            intent_correct=True,
            required_fields_complete=True,
            duplicate_question_free=True,
            dag_valid=True,
            task_coverage=1.0,
            citation_coverage=1.0,
            citation_support=1.0,
            solution_complete=1.0,
        ),
        AgentScenarioResult(
            scenario_id="change-site",
            invalidation_correct=revised.state["invalidated_task_ids"] == ["task:one"],
        ),
        AgentScenarioResult(
            scenario_id="missing-evidence",
            evidence_gap_blocked=gap.blocking and bool(gap.targeted_requirements),
        ),
        AgentScenarioResult(
            scenario_id="version-conflict",
            conflict_detected=bool(conflict.blocking and conflict.conflicts),
        ),
        AgentScenarioResult(scenario_id="restart-resume", recovery_success=True),
        AgentScenarioResult(scenario_id="parallel-sessions", session_isolated=True),
    ]
    dataset = AgentGoldenDataset.load("tests/fixtures/agent_golden_scenarios.json")
    report = AgentEvaluationRunner().run(dataset, results)

    assert approved.state["status"] == "approved"
    assert artifact.fingerprint == json.loads(artifact.to_json())["fingerprint"]
    assert revised.state["invalidated_task_ids"] == ["task:one"]
    assert gap.blocking and gap.targeted_requirements
    assert conflict.blocking and conflict.conflicts[0].evidence_ids == (
        "evidence:new",
        "evidence:old",
    )
    assert report.passed is True
