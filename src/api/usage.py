"""Own usage and audited operator metadata; no private chat administration."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from multiuser.access import AccessDenied, UserContext
from multiuser.metering import Usage
from multiuser.runs import RunConflict


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Quota(Input):
    monthly_tokens: int = Field(ge=0, le=1000000000, strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=5, max_length=500)


class Status(Input):
    enabled: bool = Field(strict=True)
    reason: str = Field(min_length=5, max_length=500)


class Membership(Status):
    user_id: uuid.UUID
    workspace_id: str = Field(min_length=1, max_length=120)
    role: Literal["member", "reviewer", "workspace_admin"]


class Reconcile(Input):
    expected_revision: int = Field(ge=1, strict=True)
    input_tokens: int = Field(ge=0, le=1000000000, strict=True)
    output_tokens: int = Field(ge=0, le=1000000000, strict=True)
    cached_tokens: int = Field(ge=0, strict=True)
    reasoning_tokens: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=5, max_length=500)


def install_usage_routes(app, identity, store, service):
    CurrentUser = Annotated[UserContext, Depends(identity)]

    def require_admin(connection):
        if not connection.execute(
            "SELECT identity_business.platform_admin() AS allowed"
        ).fetchone()["allowed"]:
            raise AccessDenied("Platform administrator required")

    def audit(connection, context, target, action, resource, reason):
        connection.execute(
            "INSERT INTO "
            "agent_business.management_audit(audit_id,actor_user_id,target_user_id,"
            "action,resource_id,reason) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (uuid.uuid4().hex, context.user_id, target, action, resource, reason),
        )

    @app.get("/v1/me/usage")
    def summary(context: CurrentUser):
        return service.summary(store, context)

    @app.get("/v1/me/usage/attempts")
    def attempts(
        context: CurrentUser,
        before: datetime | None = None,
        model: str | None = None,
        strategy: Literal["standard", "review"] | None = None,
        conversation_id: str | None = None,
    ):
        if before is not None and before.tzinfo is None:
            raise HTTPException(422, "Aware UTC cursor required")
        return service.details(
            store,
            context,
            before=before,
            model=model,
            strategy=strategy,
            conversation=conversation_id,
        )

    @app.get("/v1/admin/accounts")
    def accounts(context: CurrentUser):
        with store.transaction(context) as connection:
            require_admin(connection)
            return connection.execute(
                "SELECT u.*,coalesce(q.monthly_tokens,%s) AS "
                "monthly_tokens,coalesce(q.revision,0) AS quota_revision "
                "FROM identity_business.management_users() u LEFT JOIN "
                "agent_business.quota_limits q USING(user_id)",
                (service.monthly_tokens,),
            ).fetchall()

    @app.put("/v1/admin/accounts/{user_id}/quota")
    def set_quota(user_id: uuid.UUID, body: Quota, context: CurrentUser):
        with store.transaction(context) as connection:
            require_admin(connection)
            service.lock(connection, user_id)
            row = connection.execute(
                "SELECT * FROM agent_business.quota_limits WHERE user_id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            revision = row["revision"] if row else 0
            if revision != body.expected_revision:
                raise RunConflict("quota_changed")
            connection.execute(
                "INSERT INTO agent_business.quota_limits VALUES(%s,%s,1) ON CONFLICT(user_id) "
                "DO UPDATE SET "
                "monthly_tokens=excluded.monthly_tokens,revision=quota_limits.revision+1",
                (user_id, body.monthly_tokens),
            )
            audit(connection, context, user_id, "quota", str(user_id), body.reason)
            return {"revision": revision + 1, "monthly_tokens": body.monthly_tokens}

    @app.put("/v1/admin/accounts/{user_id}/status")
    def set_status(user_id: uuid.UUID, body: Status, context: CurrentUser):
        with store.transaction(context) as connection:
            require_admin(connection)
            connection.execute(
                "SELECT identity_business.manage_user(%s,%s,%s)",
                (user_id, body.enabled, body.reason),
            )
        return {"status": "updated"}

    @app.put("/v1/admin/memberships")
    def membership(body: Membership, context: CurrentUser):
        with store.transaction(context) as connection:
            require_admin(connection)
            connection.execute(
                "SELECT identity_business.manage_membership(%s,%s,%s,%s,%s)",
                (body.user_id, body.workspace_id, body.role, body.enabled, body.reason),
            )
        return {"status": "updated"}

    @app.get("/v1/admin/usage")
    def aggregate(context: CurrentUser):
        with store.transaction(context) as connection:
            require_admin(connection)
            return connection.execute(
                "SELECT a.owner_user_id,a.period,u.model,u.strategy,"
                "count(*) AS attempts,coalesce(sum(a.total_tokens),0) AS known_tokens,"
                "count(*) FILTER(WHERE a.source='unknown') AS unknown_attempts,"
                "u.price_json::jsonb->>'currency' AS "
                "currency,sum(a.estimated_cost)::text AS estimated_cost "
                "FROM agent_business.usage_attempts a JOIN agent_business.usage_runs u "
                "USING(run_id) "
                "GROUP BY a.owner_user_id,a.period,u.model,u.strategy,currency ORDER "
                "BY a.period DESC LIMIT 100"
            ).fetchall()

    @app.get("/v1/admin/usage/unresolved")
    def unresolved(context: CurrentUser):
        with store.transaction(context) as connection:
            require_admin(connection)
            return connection.execute(
                "SELECT "
                "attempt_id,owner_user_id,run_id,period,allocated,status,source,"
                "revision,started_at "
                "FROM agent_business.usage_attempts WHERE source IN "
                "('unknown','estimated') AND status<>'inflight' ORDER BY started_at LIMIT 100"
            ).fetchall()

    @app.post("/v1/admin/usage/{attempt_id}/reconcile")
    def reconcile(attempt_id: str, body: Reconcile, context: CurrentUser):
        if body.cached_tokens > body.input_tokens or body.reasoning_tokens > body.output_tokens:
            raise HTTPException(422, "Token subsets are inconsistent")
        with store.transaction(context) as connection:
            require_admin(connection)
            row = connection.execute(
                "SELECT * FROM agent_business.usage_attempts WHERE attempt_id=%s", (attempt_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Record not found")
            service.lock(connection, row["owner_user_id"])
            row = connection.execute(
                "SELECT * FROM agent_business.usage_attempts WHERE attempt_id=%s FOR UPDATE",
                (attempt_id,),
            ).fetchone()
            if (
                row["status"] == "inflight"
                or row["revision"] != body.expected_revision
                or row["source"]
                not in {
                    "unknown",
                    "estimated",
                }
            ):
                raise RunConflict("usage_changed")
            usage = Usage(
                "provider",
                body.input_tokens,
                body.output_tokens,
                body.input_tokens + body.output_tokens,
                body.cached_tokens,
                body.reasoning_tokens,
            )
            service.apply(connection, row, usage, status="returned")
            audit(
                connection,
                context,
                row["owner_user_id"],
                "usage_reconcile",
                attempt_id,
                body.reason,
            )
            return {"status": "reconciled", "revision": row["revision"] + 1}
