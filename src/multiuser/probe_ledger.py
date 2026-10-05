"""P0 durable synthetic runs/outbox and fencing; not the production repository."""

import hashlib
import uuid

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DDL = """
CREATE SCHEMA IF NOT EXISTS p0_business;
CREATE TABLE IF NOT EXISTS p0_business.runs (
 id text PRIMARY KEY, owner_key text NOT NULL, conversation_id text NOT NULL,
 idempotency_key text NOT NULL, content_hash text NOT NULL,
 delay double precision NOT NULL CHECK (delay >= 0 AND delay <= 30),
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded')),
 epoch integer NOT NULL DEFAULT 0, lease_until timestamptz,
 UNIQUE(owner_key,idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_conversation
 ON p0_business.runs(conversation_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS p0_business.outbox (
 run_id text PRIMARY KEY REFERENCES p0_business.runs(id),
 delivery_id text NOT NULL, dispatched boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS p0_business.results (
 run_id text PRIMARY KEY REFERENCES p0_business.runs(id), epoch integer NOT NULL,
 value text NOT NULL DEFAULT 'synthetic-result'
);
"""


class ProbeLedger:
    def __init__(self, dsn):
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=4, timeout=3, kwargs={"row_factory": dict_row}, open=True
        )

    def close(self):
        self.pool.close()

    def setup(self):
        with self.pool.connection() as connection:
            connection.execute(DDL, prepare=False)

    def create(self, owner_key, conversation_id, key, text, *, delay=0.02):
        digest = hashlib.sha256(text.encode()).hexdigest()
        run_id = uuid.uuid4().hex
        with self.pool.connection() as connection:
            row = connection.execute(
                "INSERT INTO p0_business.runs(id,owner_key,conversation_id,idempotency_key,"
                "content_hash,delay) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(owner_key,idempotency_key) "
                "DO NOTHING RETURNING id",
                (run_id, owner_key, conversation_id, key, digest, delay),
            ).fetchone()
            if row:
                connection.execute(
                    "INSERT INTO p0_business.outbox(run_id,delivery_id) VALUES(%s,%s)",
                    (run_id, uuid.uuid4().hex),
                )
                return run_id
            row = connection.execute(
                "SELECT * FROM p0_business.runs WHERE owner_key=%s AND idempotency_key=%s",
                (owner_key, key),
            ).fetchone()
            if row["content_hash"] != digest or row["conversation_id"] != conversation_id:
                raise ValueError("Idempotency key was used with different content")
            return row["id"]

    def pending(self):
        with self.pool.connection() as connection:
            return connection.execute(
                "SELECT run_id,delivery_id FROM p0_business.outbox WHERE NOT dispatched LIMIT 50"
            ).fetchall()

    def dispatched(self, run_id, delivery_id):
        with self.pool.connection() as connection:
            connection.execute(
                "UPDATE p0_business.outbox SET dispatched=true WHERE run_id=%s AND delivery_id=%s",
                (run_id, delivery_id),
            )

    def recover_expired(self):
        with self.pool.connection() as connection:
            rows = connection.execute(
                "UPDATE p0_business.runs SET status='queued' WHERE status='running' "
                "AND lease_until < clock_timestamp() RETURNING id"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE p0_business.outbox SET dispatched=false,delivery_id=%s WHERE run_id=%s",
                    (uuid.uuid4().hex, row["id"]),
                )
            return len(rows)

    def claim(self, run_id, *, lease_seconds=45):
        if not 1 <= lease_seconds <= 120:
            raise ValueError("Bounded probe lease required")
        with self.pool.connection() as connection:
            return connection.execute(
                "UPDATE p0_business.runs SET status='running',epoch=epoch+1,"
                "lease_until=clock_timestamp()+(%s*interval '1 second') "
                "WHERE id=%s AND status='queued' RETURNING epoch,delay,owner_key",
                (lease_seconds, run_id),
            ).fetchone()

    def finish(self, run_id, epoch):
        with self.pool.connection() as connection:
            row = connection.execute(
                "UPDATE p0_business.runs SET status='succeeded' WHERE id=%s AND epoch=%s "
                "AND status='running' AND lease_until > clock_timestamp() RETURNING id",
                (run_id, epoch),
            ).fetchone()
            if row:
                connection.execute(
                    "INSERT INTO p0_business.results(run_id,epoch) VALUES(%s,%s)", (run_id, epoch)
                )
            return bool(row)

    def get(self, run_id):
        with self.pool.connection() as connection:
            return connection.execute(
                "SELECT * FROM p0_business.runs WHERE id=%s", (run_id,)
            ).fetchone()

    def result_count(self, run_id):
        with self.pool.connection() as connection:
            return connection.execute(
                "SELECT count(*) AS n FROM p0_business.results WHERE run_id=%s", (run_id,)
            ).fetchone()["n"]
