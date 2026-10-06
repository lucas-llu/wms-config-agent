"""Owner-scoped persistent admission, outbox, leases and replayable progress.

This is P3's storage boundary, not an Agent executor or a provider quota ledger.
Every operation uses the non-owner P1 role and a short RLS transaction. Worker
integration must supply a freshly authorized context, never a queued bearer token.
"""

import json
import uuid
from dataclasses import asdict

from agents.repositories import SessionNotFoundError, SessionRevisionConflict
from multiuser.runs import (
    STAGES,
    LostLease,
    RunBusy,
    RunConflict,
    RunLease,
    RunLimits,
    RunRequest,
    public_run,
)
from multiuser.session_repository import PostgresSessionRepository


class RunRepository:
    def __init__(self, store, context, workspace_id, *, limits=None):
        self.sessions = PostgresSessionRepository(store, context, workspace_id)
        self.store, self.context = store, context
        self.limits = limits or RunLimits()

    def _account_lock(self, connection):
        # Serialize admission/claim across ALL workspaces of the current owner.
        connection.execute(
            "SELECT user_id FROM identity_business.users WHERE user_id=%s FOR UPDATE",
            (self.context.user_id,),
        )

    def _row(self, connection, run_id, *, lock=False):
        self.sessions._check_policy(connection)
        row = connection.execute(
            "SELECT r.* FROM agent_business.runs r JOIN agent_business.sessions s "
            "ON s.session_id=r.conversation_id WHERE r.run_id=%s AND s.workspace_id=%s"
            + (" FOR UPDATE OF r" if lock else ""),
            (run_id, self.sessions.workspace_id),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError("Run not found")
        return row

    @staticmethod
    def _event(connection, run_id, status, stage, revision=None):
        row = connection.execute(
            "UPDATE agent_business.runs SET event_sequence=event_sequence+1,stage=%s "
            "WHERE run_id=%s RETURNING event_sequence",
            (stage, run_id),
        ).fetchone()
        connection.execute(
            "INSERT INTO agent_business.run_events(run_id,sequence,status,stage,result_revision) "
            "VALUES(%s,%s,%s,%s,%s)",
            (run_id, row["event_sequence"], status, stage, revision),
        )

    def submit(self, conversation_id, request: RunRequest):
        if not isinstance(request, RunRequest):
            raise TypeError("Validated RunRequest required")
        with self.store.transaction(self.context) as connection:
            self.sessions._check_policy(connection)
            self._account_lock(connection)
            session = self.sessions._select_session(connection, conversation_id)
            row = connection.execute(
                "SELECT * FROM agent_business.runs WHERE conversation_id=%s "
                "AND owner_user_id=%s AND idempotency_key=%s",
                (conversation_id, self.context.user_id, request.idempotency_key),
            ).fetchone()
            # Replay BEFORE revision/busy/cap checks: a completed run advanced the revision.
            if row:
                if row["content_hash"] != request.fingerprint:
                    raise RunConflict("idempotency_conflict")
                return public_run(row)
            if session["current_revision"] != request.expected_revision:
                raise SessionRevisionConflict(
                    conversation_id, request.expected_revision, session["current_revision"]
                )
            if connection.execute(
                "SELECT 1 FROM agent_business.runs WHERE conversation_id=%s "
                "AND status IN ('queued','running','cancelling')",
                (conversation_id,),
            ).fetchone():
                raise RunBusy("conversation_busy")
            count = connection.execute(
                "SELECT count(*) AS n FROM agent_business.runs WHERE owner_user_id=%s "
                "AND status='queued'",
                (self.context.user_id,),
            ).fetchone()["n"]
            if count >= self.limits.pending_per_user:
                raise RunBusy("user_queue_full")
            run_id = "run:" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO agent_business.runs(run_id,conversation_id,idempotency_key,"
                "content_hash,message,answer_strategy,expected_revision,status,stage,"
                "identity_issuer,identity_subject,identity_sid,identity_iat,"
                "source_thread,queue_deadline) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,'queued','queued',"
                "%s,%s,%s,%s,%s,"
                "clock_timestamp()+(%s*interval '1 second'))",
                (
                    run_id,
                    conversation_id,
                    request.idempotency_key,
                    request.fingerprint,
                    request.message,
                    request.answer_strategy,
                    request.expected_revision,
                    self.context.issuer,
                    self.context.subject,
                    self.context.sid,
                    self.context.issued_at,
                    session.get("last_checkpoint_thread") or session["checkpoint_thread_id"],
                    self.limits.queue_seconds,
                ),
            )
            connection.execute(
                "INSERT INTO agent_business.run_outbox(run_id,delivery_id) VALUES(%s,%s)",
                (run_id, uuid.uuid4().hex),
            )
            self._event(connection, run_id, "queued", "queued")
            return public_run(self._row(connection, run_id))

    def get(self, run_id):
        with self.store.transaction(self.context) as connection:
            return public_run(self._row(connection, run_id))

    def active(self, conversation_id):
        with self.store.transaction(self.context) as connection:
            self.sessions._check_policy(connection)
            self.sessions._select_session(connection, conversation_id)
            row = connection.execute(
                "SELECT * FROM agent_business.runs WHERE conversation_id=%s "
                "AND status IN ('queued','running','cancelling')",
                (conversation_id,),
            ).fetchone()
            return public_run(row) if row else None

    def events(self, run_id, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid event cursor")
        with self.store.transaction(self.context) as connection:
            row = self._row(connection, run_id)
            if after > row["event_sequence"]:
                raise RunConflict("event_cursor_ahead")
            return connection.execute(
                "SELECT run_id,sequence,status,stage,result_revision "
                "FROM agent_business.run_events WHERE run_id=%s AND sequence>%s "
                "ORDER BY sequence LIMIT %s",
                (run_id, after, limit),
            ).fetchall()

    def pending(self, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid outbox batch")
        with self.store.transaction(self.context) as connection:
            self.sessions._check_policy(connection)
            return connection.execute(
                "SELECT o.run_id,o.delivery_id FROM agent_business.run_outbox o "
                "JOIN agent_business.runs r ON r.run_id=o.run_id "
                "JOIN agent_business.sessions s ON s.session_id=r.conversation_id "
                "WHERE NOT o.dispatched AND r.status='queued' "
                "AND r.queue_deadline>clock_timestamp() AND s.workspace_id=%s "
                "ORDER BY r.created_at,r.run_id LIMIT %s",
                (self.sessions.workspace_id, limit),
            ).fetchall()

    def dispatched(self, run_id, delivery_id):
        with self.store.transaction(self.context) as connection:
            self._row(connection, run_id, lock=True)
            return bool(
                connection.execute(
                    "UPDATE agent_business.run_outbox SET dispatched=true,deliveries=deliveries+1,"
                    "last_dispatched_at=clock_timestamp() "
                    "WHERE run_id=%s AND delivery_id=%s AND NOT dispatched RETURNING run_id",
                    (run_id, delivery_id),
                ).fetchone()
            )

    def claim(self, run_id):
        with self.store.transaction(self.context) as connection:
            self._account_lock(connection)
            row = self._row(connection, run_id, lock=True)
            if row["status"] != "queued":
                return None
            live = connection.execute(
                "SELECT queue_deadline>clock_timestamp() AS valid FROM agent_business.runs "
                "WHERE run_id=%s",
                (run_id,),
            ).fetchone()["valid"]
            if not live:
                self._end(connection, run_id, "timed_out")
                return None
            session = self.sessions._select_session(connection, row["conversation_id"])
            if session["current_revision"] != row["expected_revision"]:
                self._end(connection, run_id, "failed")
                return None
            count = connection.execute(
                "SELECT count(*) AS n FROM agent_business.runs WHERE owner_user_id=%s "
                "AND status IN ('running','cancelling')",
                (self.context.user_id,),
            ).fetchone()["n"]
            if count >= self.limits.running_per_user:
                return None
            row = connection.execute(
                "UPDATE agent_business.runs SET status='running',epoch=epoch+1,"
                "lease_until=clock_timestamp()+(%s*interval '1 second'),"
                "execution_deadline=clock_timestamp()+(%s*interval '1 second') "
                "WHERE run_id=%s RETURNING epoch,conversation_id",
                (
                    min(self.limits.lease_seconds, self.limits.execution_seconds),
                    self.limits.execution_seconds,
                    run_id,
                ),
            ).fetchone()
            thread = f"{run_id}:epoch:{row['epoch']}"
            connection.execute(
                "INSERT INTO agent_business.checkpoint_threads(thread_id,session_id,run_id,epoch) "
                "VALUES(%s,%s,%s,%s)",
                (thread, row["conversation_id"], run_id, row["epoch"]),
            )
            self._event(connection, run_id, "running", "retrieving")
            return RunLease(run_id, row["conversation_id"], row["epoch"], thread)

    def _fence(self, connection, lease):
        if not isinstance(lease, RunLease):
            raise TypeError("Server RunLease required")
        row = self._row(connection, lease.run_id, lock=True)
        valid = connection.execute(
            "SELECT lease_until>clock_timestamp() AND execution_deadline>clock_timestamp() "
            "AS valid FROM agent_business.runs WHERE run_id=%s",
            (lease.run_id,),
        ).fetchone()["valid"]
        if (
            row["status"] != "running"
            or row["epoch"] != lease.epoch
            or row["conversation_id"] != lease.conversation_id
            or lease.checkpoint_thread != f"{lease.run_id}:epoch:{lease.epoch}"
            or not valid
        ):
            raise LostLease("Run execution is no longer authorized")
        return row

    def heartbeat(self, lease):
        with self.store.transaction(self.context) as connection:
            self._fence(connection, lease)
            connection.execute(
                "UPDATE agent_business.runs SET lease_until=LEAST(execution_deadline,"
                "clock_timestamp()+(%s*interval '1 second')) WHERE run_id=%s",
                (self.limits.lease_seconds, lease.run_id),
            )

    def progress(self, lease, stage):
        if stage not in STAGES - {"queued"}:
            raise ValueError("Unknown progress stage")
        with self.store.transaction(self.context) as connection:
            row = self._fence(connection, lease)
            if row["stage"] != stage:
                self._event(connection, lease.run_id, "running", stage)

    def model_started(self, lease):
        with self.store.transaction(self.context) as connection:
            self._fence(connection, lease)
            connection.execute(
                "UPDATE agent_business.runs SET open_model_calls=open_model_calls+1,"
                "model_attempts=model_attempts+1 "
                "WHERE run_id=%s",
                (lease.run_id,),
            )

    def model_finished(self, lease):
        with self.store.transaction(self.context) as connection:
            row = self._fence(connection, lease)
            if row["open_model_calls"] < 1:
                raise RunConflict("no_model_attempt")
            connection.execute(
                "UPDATE agent_business.runs SET open_model_calls=open_model_calls-1 "
                "WHERE run_id=%s",
                (lease.run_id,),
            )

    def complete(self, lease, persist_verified_result):
        """Fence + trusted business writes + terminal event in ONE short transaction.

        The internal callback receives this connection and returns its new revision.
        Never hold this transaction while calling a model or retrieving evidence.
        A failure rolls back both business writes and the terminal transition.
        """
        with self.store.transaction(self.context) as connection:
            row = self._fence(connection, lease)
            if row["open_model_calls"]:
                raise RunConflict("model_attempt_open")
            session = self.sessions._select_session(connection, lease.conversation_id)
            if session["current_revision"] != row["expected_revision"]:
                raise RunConflict("conversation_changed")
            revision = persist_verified_result(connection)
            if type(revision) is not int or revision <= row["expected_revision"]:
                raise ValueError("New verified revision required")
            # The executor must persist both the authoritative revision and assistant turn.
            if not connection.execute(
                "SELECT 1 FROM agent_business.sessions s JOIN agent_business.turns t "
                "ON s.session_id=t.session_id AND t.revision=s.current_revision "
                "WHERE s.session_id=%s AND s.current_revision=%s AND t.role='assistant'",
                (lease.conversation_id, revision),
            ).fetchone():
                raise ValueError("Verified answer must be durably persisted")
            self._fence(connection, lease)
            connection.execute(
                "UPDATE agent_business.runs SET status='succeeded',result_revision=%s,"
                "lease_until=NULL WHERE run_id=%s",
                (revision, lease.run_id),
            )
            self._event(connection, lease.run_id, "succeeded", "succeeded", revision)
            connection.execute(
                "UPDATE agent_business.sessions SET last_checkpoint_thread=%s WHERE session_id=%s",
                (lease.checkpoint_thread, lease.conversation_id),
            )
            return public_run(self._row(connection, lease.run_id))

    @classmethod
    def _end(cls, connection, run_id, status):
        connection.execute(
            "UPDATE agent_business.runs SET status=%s,lease_until=NULL WHERE run_id=%s",
            (status, run_id),
        )
        cls._event(connection, run_id, status, status)

    def cancel(self, run_id):
        with self.store.transaction(self.context) as connection:
            row = self._row(connection, run_id, lock=True)
            if row["status"] == "queued":
                self._end(connection, run_id, "cancelled")
            elif row["status"] == "running":
                connection.execute(
                    "UPDATE agent_business.runs SET status='cancelling' WHERE run_id=%s", (run_id,)
                )
                self._event(connection, run_id, "cancelling", "cancelling")
            return public_run(self._row(connection, run_id))

    def acknowledge_cancel(self, lease):
        with self.store.transaction(self.context) as connection:
            row = self._row(connection, lease.run_id, lock=True)
            if row["status"] == "cancelling" and row["epoch"] == lease.epoch:
                self._end(connection, lease.run_id, "cancelled")
                return True
            return False

    def execution(self, lease):
        with self.store.transaction(self.context) as connection:
            return self._fence(connection, lease)

    def cached_call(self, lease, key):
        from libs.llm import ChatResponse

        with self.store.transaction(self.context) as connection:
            self._fence(connection, lease)
            row = connection.execute(
                "SELECT response_json FROM agent_business.run_model_calls WHERE run_id=%s "
                "AND call_key=%s AND status='returned' ORDER BY attempt DESC LIMIT 1",
                (lease.run_id, key),
            ).fetchone()
            return ChatResponse(**json.loads(row["response_json"])) if row else None

    def begin_call(self, lease, key):
        with self.store.transaction(self.context) as connection:
            self._fence(connection, lease)
            row = connection.execute(
                "SELECT coalesce(max(attempt),0)+1 AS n FROM agent_business.run_model_calls "
                "WHERE run_id=%s AND call_key=%s",
                (lease.run_id, key),
            ).fetchone()
            connection.execute(
                "INSERT INTO agent_business.run_model_calls VALUES(%s,%s,%s,'inflight',NULL)",
                (lease.run_id, key, row["n"]),
            )
            connection.execute(
                "UPDATE agent_business.runs SET open_model_calls=open_model_calls+1,"
                "model_attempts=model_attempts+1 WHERE run_id=%s",
                (lease.run_id,),
            )
            return row["n"]

    def finish_call(self, lease, key, attempt, response=None, *, rejected=False):
        with self.store.transaction(self.context) as connection:
            self._fence(connection, lease)
            row = connection.execute(
                "UPDATE agent_business.run_model_calls SET status=%s,response_json=%s "
                "WHERE run_id=%s AND call_key=%s AND attempt=%s "
                "AND status='inflight' RETURNING run_id",
                (
                    "rejected" if rejected else "returned",
                    None if rejected else json.dumps(asdict(response), ensure_ascii=False),
                    lease.run_id,
                    key,
                    attempt,
                ),
            ).fetchone()
            if row is None:
                raise RunConflict("model_attempt_changed")
            connection.execute(
                "UPDATE agent_business.runs SET open_model_calls=open_model_calls-1 "
                "WHERE run_id=%s",
                (lease.run_id,),
            )

    def prepared(self, lease):
        with self.store.transaction(self.context) as connection:
            self._fence(connection, lease)
            connection.execute(
                "UPDATE agent_business.runs SET resume_ready=true WHERE run_id=%s", (lease.run_id,)
            )

    def take_over(self, run_id):
        """Caller has confirmed current SID validity; unknown calls never replay."""
        with self.store.transaction(self.context) as connection:
            self._account_lock(connection)
            row = self._row(connection, run_id, lock=True)
            session = self.sessions._select_session(connection, row["conversation_id"])
            if (
                session["current_revision"] != row["expected_revision"]
                or connection.execute(
                    "SELECT 1 FROM agent_business.runs WHERE conversation_id=%s AND run_id<>%s "
                    "AND status IN ('queued','running','cancelling')",
                    (row["conversation_id"], run_id),
                ).fetchone()
            ):
                return False
            old_thread = f"{run_id}:epoch:{row['epoch']}"
            checkpoint = connection.execute(
                "SELECT 1 FROM agent_checkpoints.checkpoints WHERE thread_id=%s LIMIT 1",
                (old_thread,),
            ).fetchone()
            if row["status"] == "recovery_required" and checkpoint and not row["open_model_calls"]:
                connection.execute(
                    "UPDATE agent_business.runs SET status='queued',source_thread=%s,epoch=epoch+1,"
                    "lease_until=NULL,execution_deadline=NULL,resume_ready=true,"
                    "dispatch_after=clock_timestamp() "
                    "WHERE run_id=%s",
                    (old_thread, run_id),
                )
                connection.execute(
                    "UPDATE agent_business.run_outbox SET dispatched=false,delivery_id=%s "
                    "WHERE run_id=%s",
                    (uuid.uuid4().hex, run_id),
                )
                self._event(connection, run_id, "queued", "queued")
                return True
            return False

    def recover(self, *, limit=50):
        """Bounded reconciliation; never blindly replay an ambiguous model attempt.

        Full graph/checkpoint resume is a separate executor integration. In this
        storage stage only pre-model expired work can be safely re-dispatched.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid recovery batch")
        with self.store.transaction(self.context) as connection:
            self.sessions._check_policy(connection)
            rows = connection.execute(
                "SELECT r.* FROM agent_business.runs r JOIN agent_business.sessions s "
                "ON s.session_id=r.conversation_id WHERE s.workspace_id=%s AND "
                "((r.status='queued' AND r.queue_deadline<=clock_timestamp()) OR "
                "(r.status IN ('running','cancelling') AND "
                "(r.lease_until<=clock_timestamp() OR r.execution_deadline<=clock_timestamp()))) "
                "ORDER BY r.created_at,r.run_id LIMIT %s FOR UPDATE OF r SKIP LOCKED",
                (self.sessions.workspace_id, limit),
            ).fetchall()
            for row in rows:
                if row["status"] == "cancelling":
                    self._end(connection, row["run_id"], "cancelled")
                elif row["open_model_calls"]:
                    self._end(connection, row["run_id"], "uncertain")
                elif row["model_attempts"]:
                    self._end(connection, row["run_id"], "recovery_required")
                elif row["status"] == "queued":
                    self._end(connection, row["run_id"], "timed_out")
                else:
                    delivery = connection.execute(
                        "SELECT deliveries FROM agent_business.run_outbox WHERE run_id=%s",
                        (row["run_id"],),
                    ).fetchone()
                    if delivery["deliveries"] >= self.limits.max_deliveries:
                        self._end(connection, row["run_id"], "failed")
                    else:
                        connection.execute(
                            "UPDATE agent_business.runs SET status='queued',epoch=epoch+1,"
                            "lease_until=NULL,execution_deadline=NULL WHERE run_id=%s",
                            (row["run_id"],),
                        )
                        connection.execute(
                            "UPDATE agent_business.run_outbox SET dispatched=false,delivery_id=%s "
                            "WHERE run_id=%s",
                            (uuid.uuid4().hex, row["run_id"]),
                        )
                        self._event(connection, row["run_id"], "queued", "queued")
            return len(rows)
