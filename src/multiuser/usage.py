"""Immutable attempt accounting and token reservations; never stores prompts/tokens."""

import json
import uuid

from multiuser.metering import (
    Usage,
    billing_period,
    estimated_cost,
    normalize_usage,
    validate_price,
)
from multiuser.runs import RunConflict


class QuotaExceeded(RunConflict):
    def __init__(self):
        super().__init__("quota_exhausted")


class UsageService:
    def __init__(
        self,
        *,
        monthly_tokens=1000000,
        run_budget=100000,
        model="unconfigured",
        provider_key="unconfigured",
        price=None,
        control=None,
    ):
        if type(monthly_tokens) is not int or not 0 <= monthly_tokens <= 1000000000:
            raise ValueError("Invalid monthly token quota")
        if type(run_budget) is not int or not 1 <= run_budget <= 1000000000:
            raise ValueError("Invalid run reservation")
        self.monthly_tokens, self.run_budget = monthly_tokens, run_budget
        self.model, self.provider_key, self.price = model, provider_key, validate_price(price)
        self.control = control

    @staticmethod
    def lock(connection, user):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,9134204))", ("quota:" + str(user),)
        )

    def account(self, connection, user, period):
        connection.execute(
            "INSERT INTO agent_business.quota_limits(user_id,monthly_tokens) "
            "VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (user, self.monthly_tokens),
        )
        connection.execute(
            "INSERT INTO agent_business.quota_accounts(user_id,period) "
            "VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (user, period),
        )
        return connection.execute(
            "SELECT a.*,l.monthly_tokens FROM agent_business.quota_accounts a "
            "JOIN agent_business.quota_limits l USING(user_id) WHERE a.user_id=%s AND a.period=%s "
            "FOR UPDATE OF a",
            (user, period),
        ).fetchone()

    def reserve_run(self, connection, run):
        user, period = run["owner_user_id"], billing_period()
        self.lock(connection, user)
        if connection.execute(
            "SELECT 1 FROM agent_business.usage_runs WHERE run_id=%s", (run["run_id"],)
        ).fetchone():
            return
        connection.execute(
            "INSERT INTO "
            "agent_business.usage_runs(run_id,owner_user_id,conversation_id,workspace_id,"
            "strategy,budget,model,provider_key,price_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run["run_id"],
                user,
                run["conversation_id"],
                run["workspace_id"],
                run["answer_strategy"],
                self.run_budget,
                self.model,
                self.provider_key,
                json.dumps(self.price) if self.price else None,
            ),
        )
        self.new_reservation(connection, run["run_id"], user, period, self.run_budget)

    def new_reservation(self, connection, run_id, user, period, budget):
        account = self.account(connection, user, period)
        if account["used_tokens"] + account["held_tokens"] + budget > account["monthly_tokens"]:
            raise QuotaExceeded()
        connection.execute(
            "INSERT INTO agent_business.quota_reservations VALUES(%s,%s,%s,%s,false)",
            (run_id, period, user, budget),
        )
        connection.execute(
            "UPDATE agent_business.quota_accounts SET held_tokens=held_tokens+%s "
            "WHERE user_id=%s AND period=%s",
            (budget, user, period),
        )

    def begin_attempt(self, connection, run, key, attempt, allocated, incoming):
        user, period = run["owner_user_id"], billing_period()
        self.lock(connection, user)
        usage_run = connection.execute(
            "SELECT * FROM agent_business.usage_runs WHERE run_id=%s", (run["run_id"],)
        ).fetchone()
        if usage_run is None or usage_run["closed"]:
            raise QuotaExceeded()
        reservation = connection.execute(
            "SELECT * FROM agent_business.quota_reservations WHERE run_id=%s AND period=%s",
            (run["run_id"], period),
        ).fetchone()
        if reservation is None:
            # Cross-month calls charge the actual Shanghai call month, not an old queue bucket.
            self.release_unused(connection, run["run_id"], close_run=False)
            spent = connection.execute(
                "SELECT coalesce(sum(coalesce(total_tokens,allocated)),0) AS n "
                "FROM agent_business.usage_attempts WHERE run_id=%s AND status<>'rejected'",
                (run["run_id"],),
            ).fetchone()["n"]
            budget = usage_run["budget"] - spent
            if budget <= 0:
                raise QuotaExceeded()
            self.new_reservation(connection, run["run_id"], user, period, budget)
            reservation = connection.execute(
                "SELECT * FROM agent_business.quota_reservations WHERE run_id=%s AND period=%s",
                (run["run_id"], period),
            ).fetchone()
        pending = connection.execute(
            "SELECT coalesce(sum(allocated),0) AS n FROM agent_business.usage_attempts "
            "WHERE run_id=%s AND period=%s AND source='unknown'",
            (run["run_id"], period),
        ).fetchone()["n"]
        if reservation["closed"] or allocated > reservation["held_tokens"] - pending:
            raise QuotaExceeded()
        connection.execute(
            "INSERT INTO "
            "agent_business.usage_attempts(attempt_id,run_id,owner_user_id,call_key,attempt,"
            "period,allocated,input_estimate,status,source) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'inflight','unknown')",
            (uuid.uuid4().hex, run["run_id"], user, key, attempt, period, allocated, incoming),
        )

    def record(self, run_id, key, attempt, response=None, *, rejected=False):
        if self.control is None:
            raise RuntimeError("Accounting writer required")
        with self.control.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_business.usage_attempts "
                "WHERE run_id=%s AND call_key=%s AND attempt=%s",
                (run_id, key, attempt),
            ).fetchone()
            if row is None:
                raise RuntimeError("Unadmitted model attempt")
            self.lock(connection, row["owner_user_id"])
            row = connection.execute(
                "SELECT * FROM agent_business.usage_attempts WHERE attempt_id=%s FOR UPDATE",
                (row["attempt_id"],),
            ).fetchone()
            if row["status"] in {"returned", "rejected"}:
                return
            usage = (
                Usage("rejected", 0, 0, 0)
                if rejected
                else normalize_usage(
                    response.metadata.get("usage"),
                    input_estimate=row["input_estimate"],
                    output_estimate=len(response.content.encode()),
                )
            )
            self.apply(
                connection,
                row,
                usage,
                status="rejected" if rejected else "returned",
                request_id=None if rejected else response.metadata.get("provider_request_id"),
            )

    def apply(self, connection, row, usage, *, status, request_id=None):
        owner, period, run_id = row["owner_user_id"], row["period"], row["run_id"]
        reservation = connection.execute(
            "SELECT * FROM agent_business.quota_reservations "
            "WHERE run_id=%s AND period=%s FOR UPDATE",
            (run_id, period),
        ).fetchone()
        config = connection.execute(
            "SELECT price_json,closed FROM agent_business.usage_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        charge = usage.total_tokens
        cost = estimated_cost(
            usage, json.loads(config["price_json"]) if config["price_json"] else None
        )
        if charge is not None:
            previous_charge = row["total_tokens"] if row["source"] != "unknown" else 0
            if previous_charge is None:
                previous_charge = 0
            if row["source"] == "unknown":
                removed = min(
                    reservation["held_tokens"],
                    row["allocated"] if reservation["closed"] else charge,
                )
            else:
                removed = min(reservation["held_tokens"], max(0, charge - previous_charge))
            connection.execute(
                "UPDATE agent_business.quota_accounts SET used_tokens=used_tokens+%s,"
                "held_tokens=held_tokens-%s WHERE user_id=%s AND period=%s",
                (charge - previous_charge, removed, owner, period),
            )
            connection.execute(
                "UPDATE agent_business.quota_reservations SET held_tokens=held_tokens-%s "
                "WHERE run_id=%s AND period=%s",
                (removed, run_id, period),
            )
        request_id = request_id if isinstance(request_id, str) and len(request_id) <= 128 else None
        connection.execute(
            "UPDATE agent_business.usage_attempts SET "
            "status=%s,source=%s,input_tokens=%s,output_tokens=%s,"
            "total_tokens=%s,cached_tokens=%s,reasoning_tokens=%s,estimated_cost=%s,provider_request_id=%s,"
            "revision=revision+1,finished_at=clock_timestamp() WHERE attempt_id=%s",
            (
                status,
                usage.source,
                usage.input_tokens,
                usage.output_tokens,
                usage.total_tokens,
                usage.cached_tokens,
                usage.reasoning_tokens,
                cost,
                request_id,
                row["attempt_id"],
            ),
        )

    def release_unused(self, connection, run_id, *, close_run=True):
        rows = connection.execute(
            "SELECT * FROM agent_business.quota_reservations WHERE run_id=%s "
            "AND NOT closed ORDER BY period FOR UPDATE",
            (run_id,),
        ).fetchall()
        for row in rows:
            pending = connection.execute(
                "SELECT coalesce(sum(allocated),0) AS n FROM agent_business.usage_attempts "
                "WHERE run_id=%s AND period=%s AND source='unknown'",
                (run_id, row["period"]),
            ).fetchone()["n"]
            retained = min(row["held_tokens"], pending)
            connection.execute(
                "UPDATE agent_business.quota_accounts SET held_tokens=held_tokens-%s "
                "WHERE user_id=%s AND period=%s",
                (row["held_tokens"] - retained, row["owner_user_id"], row["period"]),
            )
            connection.execute(
                "UPDATE agent_business.quota_reservations SET held_tokens=%s,closed=true "
                "WHERE run_id=%s AND period=%s",
                (retained, run_id, row["period"]),
            )
        if close_run:
            connection.execute(
                "UPDATE agent_business.usage_runs SET closed=true WHERE run_id=%s", (run_id,)
            )
            connection.execute(
                "UPDATE agent_business.usage_attempts SET status='unknown' WHERE run_id=%s "
                "AND status='inflight'",
                (run_id,),
            )

    def settle(self, connection, run_id):
        row = connection.execute(
            "SELECT owner_user_id FROM agent_business.usage_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        if row:
            self.lock(connection, row["owner_user_id"])
            self.release_unused(connection, run_id)

    def reconcile_terminal(self):
        if self.control is None:
            return
        with self.control.transaction() as connection:
            rows = connection.execute(
                "SELECT u.run_id FROM agent_business.usage_runs u LEFT JOIN agent_business.runs r "
                "ON r.run_id=u.run_id WHERE NOT u.closed AND (r.run_id IS NULL OR r.status IN "
                "('succeeded','cancelled','failed','uncertain','timed_out',"
                "'authorization_required')) "
                "ORDER BY u.accepted_at LIMIT 50"
            ).fetchall()
            for row in rows:
                self.settle(connection, row["run_id"])

    def summary(self, store, context):
        period = billing_period()
        with store.transaction(context) as connection:
            self.lock(connection, context.user_id)
            account = self.account(connection, context.user_id, period)
            totals = connection.execute(
                "SELECT count(*) AS attempts,coalesce(sum(total_tokens),0) AS known_tokens,"
                "coalesce(sum(input_tokens),0) AS "
                "input_tokens,coalesce(sum(output_tokens),0) AS output_tokens,"
                "coalesce(sum(cached_tokens),0) AS "
                "cached_tokens,coalesce(sum(reasoning_tokens),0) AS reasoning_tokens,"
                "count(*) FILTER(WHERE source='unknown') AS unknown_attempts,"
                "count(*) FILTER(WHERE source='estimated') AS estimated_attempts "
                "FROM agent_business.usage_attempts WHERE owner_user_id=%s AND period=%s",
                (context.user_id, period),
            ).fetchone()
            today = connection.execute(
                "SELECT count(*) AS n FROM agent_business.usage_runs WHERE owner_user_id=%s "
                "AND (accepted_at AT TIME ZONE 'Asia/Shanghai')::date="
                "(clock_timestamp() AT TIME ZONE 'Asia/Shanghai')::date",
                (context.user_id,),
            ).fetchone()["n"]
            monthly = connection.execute(
                "SELECT count(*) AS n FROM agent_business.usage_runs WHERE owner_user_id=%s "
                "AND date_trunc('month',accepted_at AT TIME ZONE 'Asia/Shanghai')::date=%s",
                (context.user_id, period),
            ).fetchone()["n"]
            costs = connection.execute(
                "SELECT (u.price_json::jsonb)->>'currency' AS currency,"
                "sum(a.estimated_cost)::text AS amount FROM agent_business.usage_attempts a "
                "JOIN agent_business.usage_runs u USING(run_id) WHERE "
                "a.owner_user_id=%s AND a.period=%s "
                "AND a.estimated_cost IS NOT NULL GROUP BY currency",
                (context.user_id, period),
            ).fetchall()
            return {
                "period": str(period),
                "timezone": "Asia/Shanghai",
                "today_requests": today,
                "month_requests": monthly,
                "quota": account["monthly_tokens"],
                "used": account["used_tokens"],
                "reserved": account["held_tokens"],
                "remaining": max(
                    0, account["monthly_tokens"] - account["used_tokens"] - account["held_tokens"]
                ),
                "costs": costs,
                "cost_label": "估算费用，不是供应商账单",
                **totals,
            }

    def details(self, store, context, *, before=None, model=None, strategy=None, conversation=None):
        with store.transaction(context) as connection:
            return connection.execute(
                "SELECT "
                "a.attempt_id,a.run_id,u.conversation_id,u.model,u.strategy,a.period,a.status,a.source,"
                "a.input_tokens,a.output_tokens,a.total_tokens,a.cached_tokens,a.reasoning_tokens,"
                "a.estimated_cost::text,u.price_json::jsonb->>'version' AS price_version,"
                "u.price_json::jsonb->>'currency' AS "
                "currency,a.revision,a.started_at,a.finished_at "
                "FROM agent_business.usage_attempts a JOIN agent_business.usage_runs u "
                "USING(run_id) "
                "WHERE a.owner_user_id=%s AND (%s::timestamptz IS NULL OR a.started_at<%s) "
                "AND (%s::text IS NULL OR u.model=%s) AND (%s::text IS NULL OR u.strategy=%s) "
                "AND (%s::text IS NULL OR u.conversation_id=%s) ORDER BY a.started_at "
                "DESC,a.attempt_id LIMIT 100",
                (
                    context.user_id,
                    before,
                    before,
                    model,
                    model,
                    strategy,
                    strategy,
                    conversation,
                    conversation,
                ),
            ).fetchall()
