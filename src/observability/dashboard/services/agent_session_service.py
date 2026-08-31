"""Privacy-safe read model for Agent session Dashboard views."""

from __future__ import annotations

from typing import Any

from agents.repositories import SessionRepository


class AgentSessionService:
    def __init__(self, repository: SessionRepository) -> None:
        self.repository = repository

    def list_rows(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                "Session": item.session_id,
                "Status": item.status.value,
                "Revision": item.current_revision,
                "Goal": item.goal,
                "Updated": item.updated_at,
            }
            for item in self.repository.list_sessions(limit=limit)
        ]

    def detail(self, session_id: str) -> dict[str, Any]:
        revision = self.repository.get_revision(session_id)
        state = revision.state
        return {
            "session_id": session_id,
            "revision": revision.revision,
            "status": revision.status.value,
            "confirmed_context": state.get("confirmed_context", {}),
            "tasks": state.get("configuration_tasks", []),
            "dependency_edges": state.get("dependency_edges", []),
            "evidence_bindings": state.get("task_evidence_bindings", []),
            "conflicts": state.get("conflicts", []),
            "findings": state.get("validation_findings", []),
            "pause_reason": state.get("pause_reason", ""),
            "approvals": [
                {
                    "revision": item.revision,
                    "decision": item.decision.value,
                    "actor": item.actor,
                    "comment": item.comment,
                    "created_at": item.created_at,
                }
                for item in self.repository.list_approvals(session_id)
            ],
        }
