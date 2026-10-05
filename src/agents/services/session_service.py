"""Use-case facade for durable configuration sessions."""

from __future__ import annotations

import uuid
from typing import Any

from agents.contracts import ConfigurationSessionState
from agents.repositories import RevisionRecord, SessionRecord, SessionRepository, TurnRecord


class SessionService:
    """Coordinate session use cases without retaining process-global business state."""

    def __init__(self, repository: SessionRepository) -> None:
        self.repository = repository

    def create_session(
        self,
        goal: str,
        *,
        session_id: str | None = None,
        initial_state: ConfigurationSessionState | None = None,
    ) -> SessionRecord:
        return self.repository.create_session(
            session_id=session_id or f"session:{uuid.uuid4().hex}",
            goal=goal,
            initial_state=initial_state,
        )

    def get_session(self, session_id: str) -> SessionRecord:
        return self.repository.get_session(session_id)

    def append_turn(
        self,
        session_id: str,
        *,
        expected_revision: int,
        role: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> TurnRecord:
        return self.repository.append_turn(
            session_id=session_id,
            expected_revision=expected_revision,
            role=role,
            message=message,
            metadata=metadata,
        )

    def update_revision(
        self,
        session_id: str,
        *,
        expected_revision: int,
        state_update: ConfigurationSessionState,
        actor: str,
        reason: str,
    ) -> RevisionRecord:
        return self.repository.update_revision(
            session_id=session_id,
            expected_revision=expected_revision,
            state_update=state_update,
            actor=actor,
            reason=reason,
        )

    def cancel_session(
        self,
        session_id: str,
        *,
        expected_revision: int,
        actor: str,
        reason: str,
    ) -> SessionRecord:
        return self.repository.cancel_session(
            session_id=session_id,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
        )
