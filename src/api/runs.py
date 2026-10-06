"""Opt-in P3 task boundary, sharing the API's trusted identity dependency."""

import json
import time
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from agents.repositories import SessionNotFoundError
from multiuser.access import UserContext
from multiuser.identity import InvalidIdentity
from multiuser.run_repository import RunRepository
from multiuser.runs import TERMINAL, RunBusy, RunConflict, RunRequest


class SubmitRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1, strict=True)
    answer_strategy: Literal["standard", "review"] = "standard"


def event_cursor(run_id, value):
    if not value:
        return 0
    prefix, separator, sequence = value.rpartition("/")
    if not separator or prefix != run_id or not sequence.isascii() or not sequence.isdecimal():
        raise ValueError("Event cursor must belong to this run")
    if len(sequence) > 18:
        raise ValueError("Event cursor too large")
    return int(sequence)


def install_run_routes(app, identity, application, limits):
    # Not enabled by the production factory until workers and full recovery are connected.
    CurrentUser = Annotated[UserContext, Depends(identity)]

    def owned(context, run_id):
        with application.store.transaction(context) as connection:
            row = connection.execute(
                "SELECT s.workspace_id FROM agent_business.runs r JOIN sessions s "
                "ON s.session_id=r.conversation_id WHERE r.run_id=%s",
                (run_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFoundError("Run not found")
        return RunRepository(application.store, context, row["workspace_id"], limits=limits)

    @app.exception_handler(RunConflict)
    async def conflict(request, exc):
        status = 429 if isinstance(exc, RunBusy) and exc.code == "user_queue_full" else 409
        return JSONResponse(status_code=status, content={"detail": exc.code})

    @app.post("/v1/conversations/{session_id}/runs", status_code=202)
    def submit(session_id: str, body: SubmitRun, context: CurrentUser):
        workspace = application.store.workspace_for(context, session_id)
        repo = RunRepository(application.store, context, workspace, limits=limits)
        return repo.submit(session_id, RunRequest(**body.model_dump()))

    @app.get("/v1/conversations/{session_id}/runs/active")
    def active(session_id: str, context: CurrentUser):
        workspace = application.store.workspace_for(context, session_id)
        return RunRepository(application.store, context, workspace, limits=limits).active(
            session_id
        )

    @app.get("/v1/runs/{run_id}")
    def get(run_id: str, context: CurrentUser):
        return owned(context, run_id).get(run_id)

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel(run_id: str, context: CurrentUser):
        return owned(context, run_id).cancel(run_id)

    @app.get("/v1/runs/{run_id}/events")
    def events(run_id: str, request: Request, context: CurrentUser):
        repo = owned(context, run_id)
        after = event_cursor(run_id, request.headers.get("Last-Event-ID", ""))
        repo.events(run_id, after=after, limit=1)

        def stream():
            cursor = after
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    request.state.identity_check()
                    records = repo.events(run_id, after=cursor)
                    for record in records:
                        cursor = record["sequence"]
                        yield (
                            f"id: {run_id}/{cursor}\nevent: progress\ndata: "
                            + json.dumps(record, separators=(",", ":"))
                            + "\n\n"
                        )
                        if record["status"] in TERMINAL:
                            return
                    current = repo.get(run_id)
                    if current["status"] in TERMINAL:
                        if cursor >= current["sequence"]:
                            return
                        continue  # Drain the next page or the terminal event committed meanwhile.
                except (
                    PermissionError,
                    SessionNotFoundError,
                    RunConflict,
                    RuntimeError,
                    InvalidIdentity,
                ):
                    return
                if not records:
                    yield ": heartbeat\n\n"
                    time.sleep(1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )
