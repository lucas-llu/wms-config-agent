"""Bounded metadata-only scheduling/transport control, never user conversation text."""

import time
import uuid
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from multiuser.access import UserContext
from multiuser.runs import RunLimits

_COLUMNS = (
    "run_id,owner_user_id,conversation_id,status,epoch,identity_issuer,identity_subject,"
    "identity_sid,identity_iat,source_thread,resume_ready,open_model_calls,model_attempts"
)


class RunControl:
    def __init__(self, dsn, *, limits=None):
        self.limits = limits or RunLimits()
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=4, timeout=3, kwargs={"row_factory": dict_row}
        )
        with self.pool.connection() as connection:
            role = connection.execute(
                "SELECT current_user AS name,rolsuper,rolbypassrls,rolcreaterole FROM pg_roles "
                "WHERE rolname=current_user"
            ).fetchone()
            owners = connection.execute(
                "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname IN ('identity_business','agent_business','agent_checkpoints') "
                "AND pg_has_role(current_user,c.relowner,'MEMBER')"
            ).fetchone()["n"]
            if (
                role["name"] != "p3_control"
                or any(role[k] for k in ("rolsuper", "rolbypassrls", "rolcreaterole"))
                or owners
            ):
                self.close()
                raise ValueError("Dedicated non-owner p3_control role required")

    def close(self):
        self.pool.close()

    @contextmanager
    def transaction(self):
        with self.pool.connection() as connection, connection.transaction():
            connection.execute("SET LOCAL search_path TO pg_catalog,agent_business")
            yield connection

    def load(self, run_id):
        with self.transaction() as connection:
            return connection.execute(
                f"SELECT {_COLUMNS} FROM agent_business.runs WHERE run_id=%s", (run_id,)
            ).fetchone()

    def context(self, row):
        return UserContext(
            str(row["owner_user_id"]),
            row["identity_issuer"],
            row["identity_subject"],
            row["identity_sid"],
            row["identity_iat"],
            int(time.time()) + self.limits.execution_seconds + 120,
        )

    def offers(self, *, limit=20):
        """One head per owner, oldest last dispatch first; durable across Redis restarts."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Bounded dispatch batch required")
        with self.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(9134201)")
            rows = connection.execute(
                "WITH candidates AS (SELECT run_id,owner_user_id,created_at,"
                "row_number() OVER(PARTITION BY owner_user_id ORDER BY created_at,run_id) AS n "
                "FROM runs r WHERE status='queued' AND queue_deadline>clock_timestamp() "
                "AND dispatch_after<=clock_timestamp() AND (SELECT count(*) FROM runs a "
                "WHERE a.owner_user_id=r.owner_user_id "
                "AND a.status IN ('running','cancelling'))<%s) "
                "SELECT c.run_id,c.owner_user_id FROM candidates c LEFT JOIN dispatch_owners d "
                "ON d.owner_user_id=c.owner_user_id WHERE c.n=1 "
                "ORDER BY d.last_dispatched NULLS FIRST,c.created_at,c.run_id LIMIT %s",
                (self.limits.running_per_user, limit),
            ).fetchall()
            offers = []
            for row in rows:
                existing = connection.execute(
                    "SELECT * FROM run_outbox WHERE run_id=%s FOR UPDATE", (row["run_id"],)
                ).fetchone()
                delivery = uuid.uuid4().hex if existing["dispatched"] else existing["delivery_id"]
                connection.execute(
                    "UPDATE run_outbox SET delivery_id=%s,dispatched=false WHERE run_id=%s",
                    (delivery, row["run_id"]),
                )
                connection.execute(
                    "UPDATE runs SET dispatch_after=clock_timestamp()+interval '10 seconds' "
                    "WHERE run_id=%s",
                    (row["run_id"],),
                )
                connection.execute(
                    "INSERT INTO dispatch_owners VALUES(%s,clock_timestamp()) "
                    "ON CONFLICT(owner_user_id) "
                    "DO UPDATE SET last_dispatched=excluded.last_dispatched",
                    (row["owner_user_id"],),
                )
                offers.append({"run_id": row["run_id"], "delivery_id": delivery})
            return offers

    def dispatched(self, run_id, delivery_id):
        with self.transaction() as connection:
            connection.execute(
                "UPDATE run_outbox SET dispatched=true,deliveries=deliveries+1,"
                "last_dispatched_at=clock_timestamp() WHERE run_id=%s AND delivery_id=%s "
                "AND NOT dispatched",
                (run_id, delivery_id),
            )

    def expired(self, *, limit=50):
        with self.transaction() as connection:
            return connection.execute(
                f"SELECT {_COLUMNS} FROM runs WHERE "
                "(status='queued' AND queue_deadline<=clock_timestamp()) OR "
                "status='recovery_required' OR (status IN ('running','cancelling') "
                "AND (lease_until<=clock_timestamp() "
                "OR execution_deadline<=clock_timestamp())) ORDER BY created_at,run_id LIMIT %s",
                (limit,),
            ).fetchall()

    def stop(self, run_id, epoch, status):
        if status not in {"failed", "authorization_required", "uncertain", "cancelled"}:
            raise ValueError("Invalid control transition")
        with self.transaction() as connection:
            row = connection.execute(
                "UPDATE runs SET status=%s,stage=%s,lease_until=NULL,"
                "event_sequence=event_sequence+1 WHERE run_id=%s AND epoch=%s "
                "AND status IN ('queued','running','cancelling','recovery_required') "
                "RETURNING event_sequence",
                (status, status, run_id, epoch),
            ).fetchone()
            if row:
                connection.execute(
                    "INSERT INTO run_events(run_id,sequence,status,stage) VALUES(%s,%s,%s,%s)",
                    (run_id, row["event_sequence"], status, status),
                )
