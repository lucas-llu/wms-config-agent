"""Run-local staging: no partial user turn or business revision escapes execution."""

from dataclasses import replace
from datetime import UTC, datetime

from agents.contracts import SessionStatus, state_fingerprint
from agents.repositories import RevisionRecord, TurnRecord


class StagedSessionRepository:
    def __init__(self, repository, session_id, run_id):
        self.repository, self.session_id, self.run_id = repository, session_id, run_id
        self.workspace, self.workspace_id = repository.workspace, repository.workspace_id
        self.session = repository.get_session(session_id)
        self.base_revision = self.session.current_revision
        self.original = repository.get_revision(session_id, self.base_revision)
        self.turns = list(repository.list_turns(session_id))
        self.pending, self.revision = [], None

    def create_session(self, *, goal, session_id, initial_state=None, **kwargs):
        if session_id != self.session_id:
            raise ValueError("Staged conversation binding changed")
        self.session = replace(self.session, goal=goal)
        return self.session

    def get_session(self, session_id):
        if session_id != self.session_id:
            raise ValueError("Staged conversation binding changed")
        return self.session

    def get_revision(self, session_id, revision):
        self.get_session(session_id)
        return (
            self.revision if self.revision and self.revision.revision == revision else self.original
        )

    def list_turns(self, session_id):
        self.get_session(session_id)
        return tuple(self.turns)

    def append_turn(self, *, session_id, expected_revision, role, message, metadata=None):
        self.get_session(session_id)
        if self.session.current_revision != expected_revision or role not in {"user", "assistant"}:
            raise ValueError("Invalid staged turn")
        for existing in self.pending:
            if existing.role == role:
                if existing.message != message:
                    raise ValueError("Staged turn changed during recovery")
                return existing
        turn = TurnRecord(
            f"turn:{self.run_id}:{role}",
            session_id,
            expected_revision,
            max((t.sequence for t in self.turns), default=0) + 1,
            role,
            message,
            metadata or {},
            datetime.now(UTC).isoformat(),
        )
        self.turns.append(turn)
        self.pending.append(turn)
        return turn

    def update_revision(self, *, session_id, expected_revision, state_update, actor, reason):
        self.get_session(session_id)
        if expected_revision != self.base_revision or self.revision is not None:
            raise ValueError("Only one verified revision per run")
        state = {**self.original.state, **state_update, "revision": expected_revision + 1}
        self.workspace.validate_state(state)
        status = SessionStatus(state["status"])
        self.revision = RevisionRecord(
            session_id,
            expected_revision + 1,
            status,
            state,
            state_fingerprint(state),
            actor,
            reason,
            datetime.now(UTC).isoformat(),
        )
        self.session = replace(self.session, current_revision=expected_revision + 1, status=status)
        return self.revision

    def user_turn(self, message):
        if not self.pending:
            self.append_turn(
                session_id=self.session_id,
                expected_revision=self.base_revision,
                role="user",
                message=message,
            )

    def persist(self, connection):
        if self.revision is None or [t.role for t in self.pending] != ["user", "assistant"]:
            raise ValueError("Only completed/paused verified turns can be committed")
        with self.repository.store.bind(self.repository.context, connection):
            user, assistant = self.pending
            self.repository.append_turn(
                session_id=self.session_id,
                expected_revision=self.base_revision,
                role="user",
                message=user.message,
                metadata=user.metadata,
                turn_id=user.turn_id,
            )
            revision = self.repository.update_revision(
                session_id=self.session_id,
                expected_revision=self.base_revision,
                state_update=self.revision.state,
                actor=self.revision.actor,
                reason=self.revision.reason,
            )
            self.repository.append_turn(
                session_id=self.session_id,
                expected_revision=revision.revision,
                role="assistant",
                message=assistant.message,
                metadata=assistant.metadata,
                turn_id=assistant.turn_id,
            )
        return revision.revision
