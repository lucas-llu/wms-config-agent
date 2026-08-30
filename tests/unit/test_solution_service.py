from __future__ import annotations

import json

import pytest

from agents import ReviewDecision
from agents.repositories import SessionRepository, SessionRevisionConflict
from agents.services import SolutionService, SolutionStateError


def _state(session_id: str):
    return {
        "session_id": session_id,
        "revision": 1,
        "status": "review_required",
        "user_goal": "Configure inbound appointments",
        "confirmed_context": {"site": "DC01"},
        "configuration_tasks": [
            {"task_id": "task:one", "title": "Plan capacity", "goal": "Set capacity"}
        ],
        "evidence_registry": [
            {"evidence_id": "evidence:one", "source": "manual.pdf", "excerpt": "Supported"}
        ],
        "task_evidence_bindings": [],
        "conflicts": [],
        "validation_findings": [],
        "knowledge_fingerprint": "knowledge:one",
        "validation_fingerprint": "validation:one",
    }


def test_approval_export_is_deterministic_idempotent_and_revision_safe(tmp_path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    repository.create_session(
        session_id="session:solution",
        goal="Configure inbound appointments",
        initial_state=_state("session:solution"),
    )
    service = SolutionService(repository, tmp_path / "exports")

    approved = service.review(
        "session:solution",
        expected_revision=1,
        decision=ReviewDecision.APPROVE,
        actor="reviewer",
        comment="Approved",
    )
    first = service.export("session:solution", expected_revision=2, format="json")
    second = service.export("session:solution", expected_revision=2, format="json")
    markdown = service.export("session:solution", expected_revision=2, format="markdown")

    assert approved.state["status"] == "approved"
    assert first == second
    assert len(repository.list_exports("session:solution")) == 2
    assert (
        json.loads((tmp_path / "exports/session_solution/r2.json").read_text())["goal"]
        == "Configure inbound appointments"
    )
    assert (
        (tmp_path / "exports/session_solution/r2.md")
        .read_text()
        .startswith("# Configuration Solution")
    )
    assert markdown.fingerprint != first.fingerprint
    with pytest.raises(SessionRevisionConflict):
        service.review(
            "session:solution",
            expected_revision=1,
            decision=ReviewDecision.APPROVE,
            actor="reviewer",
            comment="stale",
        )


def test_revise_invalidates_outputs_and_pre_validation_export_is_rejected(tmp_path) -> None:
    repository = SessionRepository(tmp_path / "sessions.db")
    repository.create_session(
        session_id="session:revise",
        goal="Configure inbound appointments",
        initial_state=_state("session:revise"),
    )
    service = SolutionService(repository, tmp_path / "exports")

    with pytest.raises(SolutionStateError, match="approved"):
        service.export("session:revise", expected_revision=1, format="json")
    revised = service.review(
        "session:revise",
        expected_revision=1,
        decision=ReviewDecision.REVISE,
        actor="reviewer",
        comment="Change the site",
    )

    assert revised.state["status"] == "planning"
    assert revised.state["invalidated_task_ids"] == ["task:one"]
    assert revised.state["evidence_registry"] == []
    assert revised.state["draft_version"] == 2
