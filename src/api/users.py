"""P1 network boundary: verified identity, current session and owned repositories."""

import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.repositories import SessionNotFoundError, SessionRevisionConflict
from multiuser.access import AccessDenied, UserContext
from multiuser.identity import IdentityUnavailable, InvalidIdentity
from multiuser.session_repository import PostgresSessionRepository


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Input):
    goal: str = Field(min_length=1, max_length=16000)
    workspace_id: str
    answer_strategy: Literal["standard", "review"] = "standard"


class Rename(Input):
    title: str = Field(min_length=1, max_length=120)


class Profile(Input):
    nickname: str = Field(max_length=80)


class RevisionInput(Input):
    expected_revision: int = Field(ge=1, strict=True)


class Export(RevisionInput):
    format: Literal["markdown", "json"] = "markdown"


class Purge(Input):
    expected_revisions: dict[str, int] = Field(min_length=1, max_length=100)


class RPC(Input):
    jsonrpc: Literal["2.0"]
    id: int | str | None = None
    method: str
    params: dict = Field(default_factory=dict)


class Continue(Input):
    message: str = Field(min_length=1, max_length=16000)
    expected_revision: int = Field(ge=1, strict=True)
    answer_strategy: Literal["standard", "review"] = "standard"


class Review(Input):
    expected_revision: int = Field(ge=1, strict=True)
    decision: Literal["approve", "revise", "reject"]
    comment: str = Field(min_length=1, max_length=4000)


class Feedback(Input):
    revision: int = Field(ge=1, strict=True)
    kind: str
    reason: str = ""


class Tool(Input):
    name: str
    arguments: dict


def create_app(
    verifier, introspector, application, *, allowed_origins=(), account=None, web_config=None
):
    @asynccontextmanager
    async def lifespan(app):
        yield
        verifier.close()
        introspector.close()
        if account:
            account.close()
        if getattr(app.state, "user_store", None) is not None:
            app.state.user_store.close()

    app = FastAPI(title="WMS authenticated user API", lifespan=lifespan)

    @app.middleware("http")
    async def boundary(request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return JSONResponse(status_code=403, content={"detail": "Origin not permitted"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    bearer = HTTPBearer(auto_error=False)

    def identity(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None:
            raise HTTPException(401, "Authentication required")
        try:
            principal = verifier.verify(credentials.credentials)
            introspector.check(credentials.credentials, principal)
            request.state.access_token = credentials.credentials
            request.state.identity_check = lambda: introspector.check(
                credentials.credentials, principal
            )
            return application.store.resolve(principal)
        except InvalidIdentity as exc:
            raise HTTPException(401, "Invalid session") from exc
        except IdentityUnavailable as exc:
            raise HTTPException(503, "Identity service unavailable") from exc

    @app.exception_handler(AccessDenied)
    async def access_error(request, exc):
        return JSONResponse(status_code=403, content={"detail": "Operation not permitted"})

    @app.exception_handler(SessionNotFoundError)
    async def absent(request, exc):
        return JSONResponse(status_code=404, content={"detail": "Resource not found"})

    @app.exception_handler(SessionRevisionConflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": "Conversation changed; refresh"})

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.exception_handler(RuntimeError)
    async def unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Execution unavailable"})

    CurrentUser = Annotated[UserContext, Depends(identity)]

    @app.exception_handler(InvalidIdentity)
    async def ended_identity(request, exc):
        return JSONResponse(status_code=401, content={"detail": "Invalid session"})

    @app.exception_handler(IdentityUnavailable)
    async def unavailable_identity(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Identity service unavailable"})

    @app.get("/v1/web-config")
    def public_config():
        if web_config is None:
            raise HTTPException(503, "Web identity configuration unavailable")
        return web_config

    @app.get("/v1/me")
    def me(request: Request, context: CurrentUser):
        profile = application.store.profile(context)
        if account:
            profile.update(account.profile(context, request.state.access_token))
        return profile

    @app.get("/v1/me/devices")
    def devices(request: Request, context: CurrentUser):
        if account is None:
            raise HTTPException(503, "Account service unavailable")
        return account.devices(context, request.state.access_token)

    @app.delete("/v1/me/devices/{device_id}")
    def revoke_device(device_id: str, request: Request, context: CurrentUser):
        if account is None:
            raise HTTPException(503, "Account service unavailable")
        account.logout_device(context, request.state.access_token, device_id)
        return {"status": "signed_out"}

    @app.post("/v1/me/logout-others")
    def logout_others(request: Request, context: CurrentUser):
        if account is None:
            raise HTTPException(503, "Account service unavailable")
        account.logout_others(context, request.state.access_token)
        return {"status": "others_signed_out"}

    @app.patch("/v1/me")
    def profile(body: Profile, context: CurrentUser):
        application.store.rename(context, body.nickname)
        return application.store.profile(context)

    @app.post("/v1/logout")
    def logout(context: CurrentUser):
        application.store.revoke(context)
        return {"status": "signed_out"}

    @app.get("/v1/conversations")
    def conversations(workspace_id: str, context: CurrentUser, q: str = "", archived: bool = False):
        repo = PostgresSessionRepository(application.store, context, workspace_id)
        return [asdict(s) for s in repo.search_sessions(q, archived=archived)]

    @app.get("/v1/trash")
    def trash(workspace_id: str, context: CurrentUser):
        repo = PostgresSessionRepository(application.store, context, workspace_id)
        return [asdict(s) for s in repo.list_deleted_sessions()]

    @app.post("/v1/trash/purge")
    def purge(workspace_id: str, body: Purge, context: CurrentUser):
        repo = PostgresSessionRepository(application.store, context, workspace_id)
        return {"purged": repo.purge_deleted_sessions(body.expected_revisions)}

    @app.post("/v1/conversations")
    def start(body: Start, request: Request, context: CurrentUser):
        return application.start(
            context, **body.model_dump(), identity_check=request.state.identity_check
        )

    @app.get("/v1/conversations/{session_id}")
    def get(session_id: str, context: CurrentUser):
        return application.get(context, session_id)

    @app.get("/v1/conversations/{session_id}/workbench")
    def workbench(session_id: str, context: CurrentUser, revision: int | None = None):
        return application.workbench(context, session_id, revision)

    @app.get("/v1/conversations/{session_id}/turns/{turn_id}/evidence/{index}/images/{ordinal}")
    def evidence_image(
        session_id: str, turn_id: str, index: int, ordinal: int, context: CurrentUser
    ):
        path, mime = application.evidence_image(context, session_id, turn_id, index, ordinal)
        return FileResponse(path, media_type=mime, content_disposition_type="inline")

    @app.get("/v1/conversations/{session_id}/revisions/{revision}")
    def revision(session_id: str, revision: int, context: CurrentUser):
        return asdict(
            application.repository(context, session_id).get_revision(session_id, revision)
        )

    @app.patch("/v1/conversations/{session_id}")
    def rename(session_id: str, body: Rename, context: CurrentUser):
        application.repository(context, session_id).rename_session(session_id, body.title)
        return {"status": "renamed"}

    @app.delete("/v1/conversations/{session_id}")
    def delete(session_id: str, context: CurrentUser):
        application.repository(context, session_id).delete_session(session_id)
        return {"status": "deleted"}

    @app.post("/v1/conversations/{session_id}/restore")
    def restore(session_id: str, context: CurrentUser):
        return asdict(application.repository(context, session_id).restore_session(session_id))

    @app.post("/v1/conversations/{session_id}/archive")
    def archive(session_id: str, context: CurrentUser):
        application.repository(context, session_id).archive(session_id)
        return {"status": "archived"}

    @app.post("/v1/conversations/{session_id}/unarchive")
    def unarchive(session_id: str, context: CurrentUser):
        application.repository(context, session_id).archive(session_id, False)
        return {"status": "unarchived"}

    @app.post("/v1/conversations/{session_id}/continue")
    def continue_session(session_id: str, body: Continue, request: Request, context: CurrentUser):
        return application.continue_session(
            context, session_id, **body.model_dump(), identity_check=request.state.identity_check
        )

    @app.post("/v1/conversations/{session_id}/review")
    def review(session_id: str, body: Review, context: CurrentUser):
        return asdict(application.review(context, session_id, **body.model_dump()))

    @app.post("/v1/conversations/{session_id}/validate")
    def validate(session_id: str, body: RevisionInput, context: CurrentUser):
        return asdict(application.validate(context, session_id, **body.model_dump()))

    @app.post("/v1/conversations/{session_id}/exports")
    def export(session_id: str, body: Export, context: CurrentUser):
        return application.export(context, session_id, **body.model_dump())

    @app.get("/v1/conversations/{session_id}/exports")
    def exports(session_id: str, context: CurrentUser):
        return [
            {
                "export_id": r.export_id,
                "revision": r.revision,
                "format": r.artifact.format,
                "fingerprint": r.artifact.fingerprint,
            }
            for r in application.repository(context, session_id).list_exports(session_id)
        ]

    @app.get("/v1/conversations/{session_id}/exports/{export_id}/download")
    def export_download(session_id: str, export_id: str, context: CurrentUser):
        path = application.export_file(context, session_id, export_id)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.post("/v1/conversations/{session_id}/feedback")
    def feedback(session_id: str, body: Feedback, context: CurrentUser):
        from multiuser.feedback import PostgresFeedbackRepository

        return PostgresFeedbackRepository(application.repository(context, session_id)).record(
            session_id, **body.model_dump()
        )

    @app.get("/v1/conversations/{session_id}/feedback/{revision}")
    def feedback_summary(session_id: str, revision: int, context: CurrentUser):
        from multiuser.feedback import PostgresFeedbackRepository

        return PostgresFeedbackRepository(application.repository(context, session_id)).summary(
            session_id, revision
        )

    @app.get("/v1/conversations/{session_id}/events")
    def events(session_id: str, context: CurrentUser):
        repo = application.repository(context, session_id)
        records = repo.list_resources(session_id, "event")

        def stream():
            for record in records:
                # Reauthorize each persisted event, not just the initial request.
                try:
                    row = repo.get_resource(session_id, record["resource_id"], "event")
                except (AccessDenied, SessionNotFoundError):
                    return
                yield "data: " + row["payload_json"] + "\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/v1/conversations/{session_id}/resources/{kind}/{resource_id}")
    def resource(session_id: str, kind: str, resource_id: str, context: CurrentUser):
        if kind not in {"run", "event", "usage", "attachment", "evidence", "image"}:
            raise HTTPException(404, "Resource not found")
        row = application.repository(context, session_id).get_resource(
            session_id, resource_id, kind
        )
        return json.loads(row["payload_json"])

    @app.get("/v1/conversations/{session_id}/files/{kind}/{resource_id}")
    def file(session_id: str, kind: str, resource_id: str, context: CurrentUser):
        if kind not in {"attachment", "image"}:
            raise HTTPException(404, "Resource not found")
        path = application.file(context, session_id, resource_id, kind)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.post("/v1/tools/call")
    def tool(body: Tool, request: Request, context: CurrentUser):
        return application.tool(
            context, body.name, body.arguments, identity_check=request.state.identity_check
        )

    @app.get("/mcp")
    def mcp_get(context: CurrentUser):
        raise HTTPException(405, "This stateless tool adapter has no server notification stream")

    @app.post("/mcp")
    def mcp(body: RPC, request: Request, context: CurrentUser):
        version = request.headers.get("mcp-protocol-version", "2025-03-26")
        if version not in {"2025-03-26", "2025-11-25"}:
            raise HTTPException(400, "Unsupported protocol version")
        if body.method == "notifications/initialized" and body.id is None:
            return Response(status_code=202)
        if body.id is None:
            raise HTTPException(400, "Request ID required")
        if body.method == "initialize":
            selected = body.params.get("protocolVersion", "2025-11-25")
            result = {
                "protocolVersion": selected
                if selected in {"2025-03-26", "2025-11-25"}
                else "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "wms-user-tools", "version": "0.1.0"},
            }
        elif body.method == "ping":
            result = {}
        elif body.method == "tools/list":
            from multiuser.tools import schemas

            result = {"tools": schemas()}
        elif body.method == "tools/call":
            try:
                arguments = Tool.model_validate(body.params)
                from multiuser.tools import parse

                parsed = parse(arguments.name, arguments.arguments)
            except (ValueError, ValidationError):
                return {
                    "jsonrpc": "2.0",
                    "id": body.id,
                    "error": {"code": -32602, "message": "Invalid tool or parameters"},
                }
            try:
                data = jsonable_encoder(
                    application.tool(
                        context, arguments.name, parsed, identity_check=request.state.identity_check
                    )
                )
            except (
                AccessDenied,
                SessionNotFoundError,
                SessionRevisionConflict,
                ValueError,
                RuntimeError,
            ):
                return {
                    "jsonrpc": "2.0",
                    "id": body.id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Operation unavailable; refresh or check permissions.",
                            }
                        ],
                        "isError": True,
                    },
                }
            result = {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data if isinstance(data, dict) else {"items": data},
                "isError": False,
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": body.id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        return {"jsonrpc": "2.0", "id": body.id, "result": result}

    return app
