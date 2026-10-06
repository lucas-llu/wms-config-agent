"""Atomic first-message acceptance plus scoped reconnect lookup."""

import hashlib

from agents.repositories import SessionNotFoundError
from agents.services import SessionService
from multiuser.run_repository import RunRepository
from multiuser.runs import RunRequest, public_run


def new_conversation_id(context, workspace_id, key):
    return (
        "session:"
        + hashlib.sha256(f"{context.user_id}\0{workspace_id}\0{key}".encode()).hexdigest()[:32]
    )


def start_run(
    application, context, *, goal, workspace_id, answer_strategy, idempotency_key, limits
):
    request = RunRequest(goal, idempotency_key, 1, answer_strategy)
    session_id = new_conversation_id(context, workspace_id, idempotency_key)
    repo = RunRepository(application.store, context, workspace_id, limits=limits)
    with application.store.transaction(context) as connection:
        repo._account_lock(connection)
        with application.store.bind(context, connection):
            # Deterministic owner-bound ID supports retry after loss of the very first 202 response.
            try:
                repo.sessions.get_session(session_id)
            except SessionNotFoundError:
                SessionService(repo.sessions).create_session(goal, session_id=session_id)
            return repo.submit(session_id, request)


def lookup_run(application, context, workspace_id, key, conversation_id=None, *, limits=None):
    # Validate even the reconnect query, not just writes.
    RunRequest("lookup", key, 1)
    conversation_id = conversation_id or new_conversation_id(context, workspace_id, key)
    repo = RunRepository(application.store, context, workspace_id, limits=limits)
    with application.store.transaction(context) as connection:
        repo.sessions._check_policy(connection)
        row = connection.execute(
            "SELECT r.* FROM agent_business.runs r JOIN agent_business.sessions s "
            "ON s.session_id=r.conversation_id WHERE r.conversation_id=%s AND r.idempotency_key=%s "
            "AND s.workspace_id=%s",
            (conversation_id, key, workspace_id),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError("Run not found")
        return public_run(row)
