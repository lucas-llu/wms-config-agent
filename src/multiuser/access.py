"""Per-request trusted user context and transaction-local PostgreSQL identity."""

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from multiuser.identity import Principal


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class UserContext:
    user_id: str
    issuer: str
    subject: str
    sid: str
    issued_at: int
    expires_at: int


def context_values(context):
    return {
        "app.user_id": context.user_id,
        "app.issuer": context.issuer,
        "app.subject": context.subject,
        "app.sid": context.sid,
        "app.token_iat": str(context.issued_at),
        "app.token_exp": str(context.expires_at),
    }


class AccessStore:
    def __init__(self, dsn, *, max_connections=8):
        self._binding = ContextVar("owned_short_transaction", default=None)
        self.usage = None
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=max_connections,
            kwargs={"row_factory": dict_row},
            timeout=3,
            check=ConnectionPool.check_connection,
        )
        with self.pool.connection() as connection:
            role = connection.execute(
                "SELECT rolsuper,rolbypassrls,rolcreaterole FROM pg_roles "
                "WHERE rolname=current_user"
            ).fetchone()
            owners = connection.execute(
                "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname IN ('identity_business','agent_business','agent_checkpoints') "
                "AND pg_has_role(current_user,c.relowner,'MEMBER')"
            ).fetchone()["n"]
            if any(role.values()) or owners:
                self.pool.close()
                raise ValueError("Runtime database role must not own tables or bypass RLS")

    def close(self):
        self.pool.close()

    @contextmanager
    def transaction(self, context, *, require_active=True):
        if not isinstance(context, UserContext):
            raise TypeError("Trusted UserContext required")
        if bound := self._binding.get():
            bound_context, connection = bound
            if context != bound_context:
                raise AccessDenied("Cannot switch identity inside a transaction")
            if (
                require_active
                and not connection.execute(
                    "SELECT identity_business.actor_active() AS active"
                ).fetchone()["active"]
            ):
                raise AccessDenied("Account or session is unavailable")
            yield connection
            return
        with self.pool.connection() as connection, connection.transaction():
            connection.execute(
                "SET LOCAL search_path TO pg_catalog,agent_business,identity_business"
            )
            for name, value in context_values(context).items():
                connection.execute("SELECT set_config(%s,%s,true)", (name, value))
            if (
                require_active
                and not connection.execute(
                    "SELECT identity_business.actor_active() AS active"
                ).fetchone()["active"]
            ):
                raise AccessDenied("Account or session is unavailable")
            yield connection

    @contextmanager
    def bind(self, context, connection):
        """Internal short-transaction composition; NEVER span retrieval/model calls."""
        token = self._binding.set((context, connection))
        try:
            yield
        finally:
            self._binding.reset(token)

    def resolve(self, principal: Principal):
        if not principal.sid or principal.issued_at <= 0 or principal.expires_at <= 0:
            raise AccessDenied("User session context required")
        initial = UserContext(
            "",
            principal.issuer,
            principal.subject,
            principal.sid,
            principal.issued_at,
            principal.expires_at,
        )
        with self.transaction(initial, require_active=False) as connection:
            connection.execute(
                "INSERT INTO identity_business.users(user_id,identity_issuer,identity_subject) "
                "VALUES(%s,%s,%s) ON CONFLICT(identity_issuer,identity_subject) DO NOTHING",
                (uuid.uuid4(), principal.issuer, principal.subject),
            )
            row = connection.execute(
                "SELECT * FROM identity_business.users "
                "WHERE identity_issuer=%s AND identity_subject=%s",
                (principal.issuer, principal.subject),
            ).fetchone()
        context = UserContext(
            str(row["user_id"]),
            principal.issuer,
            principal.subject,
            principal.sid,
            principal.issued_at,
            principal.expires_at,
        )
        with self.transaction(context):
            pass
        return context

    def profile(self, context):
        with self.transaction(context) as connection:
            user = connection.execute(
                "SELECT user_id,nickname,status FROM identity_business.users"
            ).fetchone()
            memberships = connection.execute(
                "SELECT workspace_id,role FROM identity_business.memberships WHERE active"
            ).fetchall()
            return {**user, "workspaces": memberships}

    def workspace_for(self, context, session_id):
        with self.transaction(context) as connection:
            row = connection.execute(
                "SELECT workspace_id FROM sessions WHERE session_id=%s", (session_id,)
            ).fetchone()
            if row is None:
                from agents.repositories import SessionNotFoundError

                raise SessionNotFoundError("Conversation not found")
            return row["workspace_id"]

    def revoke(self, context):
        with self.transaction(context) as connection:
            connection.execute(
                "INSERT INTO identity_business.revoked_sessions(user_id,sid) VALUES(%s,%s) "
                "ON CONFLICT DO NOTHING",
                (context.user_id, context.sid),
            )

    def rename(self, context, nickname):
        with self.transaction(context) as connection:
            connection.execute(
                "UPDATE identity_business.users SET nickname=%s WHERE user_id=%s",
                (nickname, context.user_id),
            )

    def require_reviewer(self, context, workspace_id):
        with self.transaction(context) as connection:
            role = connection.execute(
                "SELECT role FROM identity_business.memberships WHERE workspace_id=%s AND active",
                (workspace_id,),
            ).fetchone()
            if not role or role["role"] not in {"reviewer", "workspace_admin"}:
                raise AccessDenied("Reviewer permission required")
