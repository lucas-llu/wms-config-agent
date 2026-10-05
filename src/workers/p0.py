"""RQ transport with PostgreSQL as the durable synthetic-result authority."""

import argparse
import os
import time

from redis import BlockingConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from rq import Queue, Worker
from rq.exceptions import DuplicateJobError
from rq.serializers import JSONSerializer

from multiuser.probe_ledger import ProbeLedger


def redis_client(*, worker=False):
    pool = BlockingConnectionPool.from_url(
        os.environ["P0_REDIS_URL"],
        max_connections=16,
        timeout=2,
        socket_connect_timeout=2,
        socket_timeout=90 if worker else 5,
        health_check_interval=30,
        retry=Retry(NoBackoff(), 0),
    )
    return Redis(connection_pool=pool)


def probe_queue(client):
    return Queue("wms-p0-probe", connection=client, serializer=JSONSerializer, default_timeout=30)


def dispatch_pending(ledger, queue):
    sent = 0
    for row in ledger.pending():
        try:
            queue.enqueue(
                execute_probe, row["run_id"], job_id=row["delivery_id"], result_ttl=600, unique=True
            )
        except DuplicateJobError:
            job = queue.fetch_job(row["delivery_id"])
            if (
                job is None
                or tuple(job.args) != (row["run_id"],)
                or job.func_name != "workers.p0.execute_probe"
            ):
                raise
        ledger.dispatched(row["run_id"], row["delivery_id"])
        sent += 1
    return sent


def execute_probe(run_id):
    # Construct pools inside the child process, never reuse a pre-fork socket.
    ledger = ProbeLedger(os.environ["P0_POSTGRES_DSN"])
    try:
        claim = ledger.claim(run_id, lease_seconds=int(os.getenv("P0_LEASE_SECONDS", "45")))
        if claim is None:
            return {"committed": False}
        time.sleep(claim["delay"])
        return {"committed": ledger.finish(run_id, claim["epoch"])}
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--burst", action="store_true")
    args = parser.parse_args()
    client = redis_client(worker=True)
    try:
        worker = Worker(
            [probe_queue(client)], connection=client, serializer=JSONSerializer, worker_ttl=60
        )
        worker.work(burst=args.burst, logging_level="WARNING")
    finally:
        client.close()
        client.connection_pool.disconnect()


if __name__ == "__main__":
    main()
