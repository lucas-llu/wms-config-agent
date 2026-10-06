"""Fixed JSON RQ transport; only run_id crosses Redis and the internal API."""

import argparse
import os
import re
from urllib.parse import urlsplit

import httpx
from redis import BlockingConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from rq import Queue, Worker
from rq.exceptions import DuplicateJobError
from rq.serializers import JSONSerializer


class ExecutionClient:
    def __init__(self, url, token, *, client=None):
        parsed = urlsplit(url)
        if (
            (
                parsed.scheme != "https"
                and not (parsed.scheme == "http" and parsed.hostname == "127.0.0.1")
            )
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Trusted internal HTTPS/loopback execution origin required")
        if len(token) < 32:
            raise ValueError("Strong internal execution credential required")
        self.url, self.token = url.rstrip("/"), token
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(660, connect=2),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    def close(self):
        self.client.close()

    def request(self, path, body=None):
        response = self.client.request(
            "GET" if body is None else "POST",
            self.url + path,
            headers={"Authorization": "Bearer " + self.token},
            json=body,
        )
        response.raise_for_status()
        return response.json()


def redis_client(*, worker=False):
    url = os.environ["WMS_REDIS_URL"]
    parsed = urlsplit(url)
    local_fixture = os.getenv("WMS_P3_LIVE") == "1" and parsed.hostname == "127.0.0.1"
    if not local_fixture and (
        parsed.scheme != "rediss" or not parsed.password or not parsed.username
    ):
        raise ValueError("Production Redis requires TLS and a dedicated ACL user")
    return Redis(
        connection_pool=BlockingConnectionPool.from_url(
            url,
            max_connections=8,
            timeout=2,
            socket_connect_timeout=2,
            socket_timeout=90 if worker else 5,
            health_check_interval=30,
            retry=Retry(NoBackoff(), 0),
        )
    )


def queue(client):
    return Queue("wms-runs", connection=client, serializer=JSONSerializer, default_timeout=660)


def dispatch(control, tasks):
    delivered = 0
    for item in control.offers():
        try:
            tasks.enqueue(
                execute_run,
                item["run_id"],
                job_id=item["delivery_id"],
                result_ttl=600,
                failure_ttl=600,
                unique=True,
            )
        except DuplicateJobError:
            job = tasks.fetch_job(item["delivery_id"])
            if (
                job is None
                or tuple(job.args) != (item["run_id"],)
                or job.func_name != "workers.runs.execute_run"
            ):
                raise
        control.dispatched(**item)
        delivered += 1
    return delivered


def execute_run(run_id):
    # Child creates its own pool/socket. Shared service retains model/index objects.
    client = ExecutionClient(os.environ["WMS_EXECUTION_URL"], os.environ["WMS_EXECUTION_TOKEN"])
    try:
        return client.request("/internal/execute", {"run_id": run_id})
    finally:
        client.close()


class TrustedWorker(Worker):
    def perform_job(self, job, queue):
        if (
            job.func_name != "workers.runs.execute_run"
            or len(job.args) != 1
            or job.kwargs
            or not isinstance(job.args[0], str)
            or not re.fullmatch(r"run:[a-f0-9]{32}", job.args[0])
        ):
            raise ValueError("Rejected untrusted queue task")
        return super().perform_job(job, queue)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--burst", action="store_true")
    args = parser.parse_args()
    client = redis_client(worker=True)
    try:
        TrustedWorker(
            [queue(client)], connection=client, serializer=JSONSerializer, worker_ttl=60
        ).work(burst=args.burst, logging_level="ERROR")
    finally:
        client.close()
        client.connection_pool.disconnect()


if __name__ == "__main__":
    main()
