"""P0 identity boundary. No legacy host-process tools are exposed."""

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from multiuser.identity import IdentityUnavailable, InvalidIdentity, OIDCVerifier, Principal


def create_app(verifier: OIDCVerifier) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        yield
        verifier.close()

    app = FastAPI(title="WMS multi-user P0 identity probe", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def identity(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            return verifier.verify(credentials.credentials)
        except InvalidIdentity as exc:
            raise HTTPException(401, "Invalid access token") from exc
        except IdentityUnavailable as exc:
            raise HTTPException(503, "Identity service unavailable") from exc

    @app.get("/health")
    def health():
        return {"status": "ok", "phase": "P0"}

    @app.get("/v1/me")
    def me(principal: Annotated[Principal, Depends(identity)]):
        return {
            "identity_key": principal.identity_key,
            "issuer": principal.issuer,
            "subject": principal.subject,
            "knowledge_access": [],
        }

    return app
