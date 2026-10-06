"""Internal shared executor, separate from user HTTP routing and credentials."""

import hmac
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from workers.runs import dispatch, queue


class Execute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^run:[a-f0-9]{32}$")


def create_execution_app(executor, redis, token, *, period=1):
    if len(token) < 32:
        raise ValueError("Strong internal credential required")
    ended = threading.Event()
    health = {"dispatch_failures": 0, "dispatch_ok": True}

    def tick():
        tasks = queue(redis)
        while not ended.is_set():
            try:
                executor.reconcile()
                dispatch(executor.control, tasks)
                health["dispatch_ok"] = True
            except Exception:
                # Outbox remains durable; no private exception text/credentials in logs.
                health["dispatch_failures"] += 1
                health["dispatch_ok"] = False
            ended.wait(period)

    @asynccontextmanager
    async def lifespan(app):
        thread = threading.Thread(target=tick, daemon=True)
        thread.start()
        yield
        ended.set()
        thread.join(timeout=10)
        redis.close()
        redis.connection_pool.disconnect()
        executor.authority.close()
        executor.control.close()
        executor.store.close()
        if hasattr(executor.agent.llm, "close"):
            executor.agent.llm.close()

    app = FastAPI(title="WMS internal shared execution", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        from fastapi.responses import JSONResponse

        provided = request.headers.get("authorization", "")
        if not hmac.compare_digest(provided, "Bearer " + token):
            return JSONResponse(
                status_code=401, content={"detail": "Internal authorization required"}
            )
        return await call_next(request)

    @app.get("/internal/ready")
    def ready():
        # No user requests may be accepted without control-role/governor initialization.
        if not health["dispatch_ok"]:
            raise HTTPException(503, "Dispatcher unavailable; durable tasks retained")
        return {"ready": True, "protocol": "wms-runs-v1"}

    @app.post("/internal/execute")
    def execute(body: Execute):
        result = executor.execute(body.run_id)
        if result.get("reason") == "execution_busy":
            raise HTTPException(503, "Execution capacity full")
        return result

    return app
