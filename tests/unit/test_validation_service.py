from __future__ import annotations

from agents.services import ValidationService


def _task(task_id: str = "task:one", **changes):
    value = {
        "task_id": task_id,
        "module": "inbound",
        "depends_on": [],
        "preconditions": [],
        "steps": ["Configure the inbound appointment policy"],
        "validation_steps": ["Verify appointment capacity behavior"],
        "rollback_steps": ["Restore the previous policy"],
        "evidence_requirements": ["Inbound appointment documentation"],
        "risk_level": "medium",
    }
    value.update(changes)
    return value


def _evidence(evidence_id: str = "evidence:one", **changes):
    value = {
        "evidence_id": evidence_id,
        "chunk_id": evidence_id.replace("evidence", "chunk"),
        "source": "manuals/inbound.pdf",
        "excerpt": "Supported inbound appointment configuration.",
        "score": 0.9,
        "product_version": "2024.1",
        "module": "inbound",
        "site": "DC01",
        "environment": "test",
        "collection": "wms",
    }
    value.update(changes)
    return value


def _binding(task_id="task:one", evidence_ids=None, status="supported", gaps=None):
    return {
        "task_id": task_id,
        "queries": [
            {
                "requirement": "Inbound appointment documentation",
                "query": "Standalone inbound appointment query",
                "filters": {"version": "2024.1", "module": "inbound"},
            }
        ],
        "evidence_ids": evidence_ids if evidence_ids is not None else ["evidence:one"],
        "evidence_status": status,
        "gap_reasons": gaps or [],
    }


def _validate(**changes):
    values = {
        "tasks": [_task()],
        "dependency_edges": [],
        "evidence_registry": [_evidence()],
        "bindings": [_binding()],
        "confirmed_context": {
            "product_version": "2024.1",
            "site": "DC01",
            "environment": "test",
        },
        "invalidated_task_ids": [],
    }
    values.update(changes)
    return ValidationService().validate(**values)


def test_valid_supported_draft_is_stable_and_not_blocking() -> None:
    first = _validate()
    second = _validate()

    assert first.blocking is False
    assert first.conflicts == ()
    assert first.findings == ()
    assert first.targeted_requirements == {}
    assert first.fingerprint == second.fingerprint


def test_scope_conflicts_preserve_every_source_and_block_review() -> None:
    report = _validate(
        evidence_registry=[
            _evidence("evidence:one", product_version="2023.1"),
            _evidence("evidence:two", product_version="2025.1"),
        ],
        bindings=[_binding(evidence_ids=["evidence:one", "evidence:two"])],
    )

    assert report.blocking is True
    assert len(report.conflicts) == 1
    assert report.conflicts[0].dimension == "product_version"
    assert report.conflicts[0].evidence_ids == ("evidence:one", "evidence:two")


def test_missing_rollback_validation_and_unreferenced_command_are_blocking() -> None:
    report = _validate(
        tasks=[
            _task(
                steps=["execute policy_change"],
                validation_steps=[],
                rollback_steps=[],
            )
        ],
        evidence_registry=[],
        bindings=[
            _binding(
                evidence_ids=[],
                status="unsupported",
                gaps=["insufficient_evidence"],
            )
        ],
    )

    rules = {item.rule_id for item in report.findings}
    assert {"missing_validation", "missing_rollback", "evidence_coverage"}.issubset(rules)
    assert "command_without_evidence" in rules
    assert report.targeted_requirements == {"task:one": ("Inbound appointment documentation",)}


def test_dependency_cycle_edge_mismatch_and_invalidated_tasks_are_detected() -> None:
    tasks = [
        _task("task:one", depends_on=["task:two"], preconditions=["task two"]),
        _task("task:two", depends_on=["task:one"], preconditions=["task one"]),
    ]
    bindings = [
        _binding("task:one"),
        _binding("task:two", evidence_ids=["evidence:two"]),
    ]
    report = _validate(
        tasks=tasks,
        dependency_edges=[],
        evidence_registry=[_evidence(), _evidence("evidence:two")],
        bindings=bindings,
        invalidated_task_ids=["task:one"],
    )

    rules = {item.rule_id for item in report.findings}
    assert {"dependency_cycle", "dependency_edge_mismatch", "invalidated_tasks"}.issubset(rules)
