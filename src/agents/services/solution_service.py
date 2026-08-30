"""Deterministic composition, review, revision, and export use cases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agents.contracts import (
    ExportArtifact,
    ReviewDecision,
    SessionStatus,
    canonical_json,
    stable_contract_id,
)
from agents.repositories import SessionRepository
from libs.atomic_file import replace_file_atomically


class SolutionStateError(ValueError):
    pass


class SolutionService:
    def __init__(self, repository: SessionRepository, export_root: str | Path) -> None:
        self.repository = repository
        self.export_root = Path(export_root)

    def compose(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("status") not in {
            SessionStatus.REVIEW_REQUIRED.value,
            SessionStatus.APPROVED.value,
        }:
            raise SolutionStateError("solution can be composed only after validation")
        if state.get("conflicts") or any(
            item.get("severity") == "blocking" for item in state.get("validation_findings", [])
        ):
            raise SolutionStateError("blocking validation state cannot be composed")
        return {
            "schema_version": 1,
            "session_id": state["session_id"],
            "revision": int(state["revision"]),
            "goal": state["user_goal"],
            "confirmed_context": state.get("confirmed_context", {}),
            "assumptions": state.get("assumptions", []),
            "decisions": state.get("decisions", []),
            "tasks": state.get("configuration_tasks", []),
            "task_evidence_bindings": state.get("task_evidence_bindings", []),
            "evidence": state.get("evidence_registry", []),
            "conflicts": state.get("conflicts", []),
            "validation_findings": state.get("validation_findings", []),
            "knowledge_fingerprint": state.get("knowledge_fingerprint", "knowledge:empty"),
            "validation_fingerprint": state.get("validation_fingerprint", "validation:empty"),
        }

    def review(
        self,
        session_id: str,
        *,
        expected_revision: int,
        decision: ReviewDecision,
        actor: str,
        comment: str,
    ):
        record = self.repository.get_revision(session_id, expected_revision)
        state = dict(record.state)
        if state.get("status") != SessionStatus.REVIEW_REQUIRED.value:
            raise SolutionStateError("only review_required revisions can be reviewed")
        self.repository.record_approval(
            session_id=session_id,
            expected_revision=expected_revision,
            decision=decision,
            actor=actor,
            comment=comment,
            approval_id=stable_contract_id(
                "approval",
                {"session_id": session_id, "revision": expected_revision, "decision": decision},
            ),
        )
        update: dict[str, Any] = {"review_decision": decision.value}
        if decision is ReviewDecision.APPROVE:
            update["status"] = SessionStatus.APPROVED.value
        elif decision is ReviewDecision.REJECT:
            update["status"] = SessionStatus.REJECTED.value
        else:
            update.update(
                {
                    "status": SessionStatus.PLANNING.value,
                    "invalidated_task_ids": [
                        item["task_id"] for item in state.get("configuration_tasks", [])
                    ],
                    "evidence_registry": [],
                    "task_evidence_bindings": [],
                    "conflicts": [],
                    "validation_findings": [],
                    "draft_version": int(state.get("draft_version", 1)) + 1,
                }
            )
        return self.repository.update_revision(
            session_id=session_id,
            expected_revision=expected_revision,
            state_update=update,
            actor=actor,
            reason=f"review:{decision.value}",
        )

    def export(self, session_id: str, *, expected_revision: int, format: str) -> ExportArtifact:
        record = self.repository.get_revision(session_id, expected_revision)
        state = dict(record.state)
        if state.get("status") != SessionStatus.APPROVED.value:
            raise SolutionStateError("only approved revisions can be exported")
        payload = self.compose(state)
        if format == "json":
            content = canonical_json(payload) + "\n"
        elif format == "markdown":
            content = self._markdown(payload)
        else:
            raise ValueError("format must be json or markdown")
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        destination = (
            self.export_root
            / session_id.replace(":", "_")
            / f"r{expected_revision}.{format if format == 'json' else 'md'}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        replace_file_atomically(temporary, destination)
        artifact = ExportArtifact(
            format=format, path=destination.as_posix(), fingerprint=fingerprint
        )
        export_id = stable_contract_id(
            "export", {"session_id": session_id, "revision": expected_revision, "format": format}
        )
        if not any(
            item.export_id == export_id for item in self.repository.list_exports(session_id)
        ):
            self.repository.record_export(
                session_id=session_id,
                expected_revision=expected_revision,
                artifact=artifact,
                export_id=export_id,
            )
        return artifact

    @staticmethod
    def _markdown(payload: dict[str, Any]) -> str:
        lines = [
            "# Configuration Solution",
            "",
            f"- Session: `{payload['session_id']}`",
            f"- Revision: `{payload['revision']}`",
            f"- Goal: {payload['goal']}",
            f"- Knowledge fingerprint: `{payload['knowledge_fingerprint']}`",
            f"- Validation fingerprint: `{payload['validation_fingerprint']}`",
            "",
        ]
        sections = (
            ("Confirmed Context", "confirmed_context"),
            ("Assumptions", "assumptions"),
            ("Decisions", "decisions"),
            ("Tasks", "tasks"),
            ("Task Evidence Bindings", "task_evidence_bindings"),
            ("Evidence", "evidence"),
            ("Conflicts", "conflicts"),
            ("Validation Findings", "validation_findings"),
        )
        for title, key in sections:
            lines.extend(
                [
                    f"## {title}",
                    "",
                    "```json",
                    json.dumps(payload[key], ensure_ascii=False, indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"
