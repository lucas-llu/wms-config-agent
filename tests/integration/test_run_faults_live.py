"""Hard process kills and real Redis restart; fixed synthetic services only."""

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from test_run_execution_live import accepted
from test_run_execution_live import p1_system as base_system
from test_run_execution_live import system as system_fixture
from test_run_execution_live import tokens as tokens_fixture

p1_system = base_system
system = system_fixture
tokens = tokens_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P3_LIVE") != "1" or os.name != "posix",
    reason="Real RQ hard-kill recovery requires isolated Linux services",
)


def stop(process):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


class Processes:
    def __init__(self, tmp_path):
        self.flag = tmp_path / "synthetic-boundary"
        self.executor = self.worker = None
        self.env = {
            **os.environ,
            "PYTHONPATH": str(Path("src").resolve()),
            "WMS_REDIS_URL": os.environ["P0_REDIS_URL"],
            "WMS_EXECUTION_URL": "http://127.0.0.1:8533",
            "WMS_EXECUTION_TOKEN": os.environ["P3_EXECUTION_TOKEN"],
            "P3_FIXTURE_LEASE": "5",
            "P3_FIXTURE_MODEL_KEY": "fault:" + uuid.uuid4().hex,
        }

    def start_executor(self, boundary=""):
        stop(self.executor)
        env = {**self.env, "P3_FAULT_BOUNDARY": boundary, "P3_FAULT_FLAG": str(self.flag)}
        self.executor = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.p3_fixture_app:create_fixture_executor",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8533",
                "--no-access-log",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                r = httpx.get(
                    self.env["WMS_EXECUTION_URL"] + "/internal/ready",
                    headers={"Authorization": "Bearer " + self.env["WMS_EXECUTION_TOKEN"]},
                    timeout=1,
                )
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise AssertionError("Synthetic executor readiness failed")

    def start_worker(self):
        stop(self.worker)
        self.worker = subprocess.Popen(
            [sys.executable, "-m", "workers.runs"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def close(self):
        stop(self.worker)
        stop(self.executor)


def wait(predicate, *, seconds=35):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.2)
    raise AssertionError("Synthetic fault recovery timed out")


@pytest.mark.parametrize(
    "boundary,terminal", [("after_return", "succeeded"), ("inflight", "uncertain")]
)
def test_executor_hard_kill_takes_over_known_result_but_never_unknown_call(
    system, tmp_path, boundary, terminal
):
    repo, run, _ = accepted(system)
    processes = Processes(tmp_path)
    try:
        processes.start_executor(boundary)
        processes.start_worker()
        wait(processes.flag.exists)
        row = (
            system[-1]
            .execute(
                "SELECT call_key,attempt,status FROM agent_business.run_model_calls "
                "WHERE run_id=%s",
                (run["run_id"],),
            )
            .fetchone()
        )
        assert row and row[2] == ("returned" if boundary == "after_return" else "inflight")
        stop(processes.executor)
        stop(processes.worker)
        time.sleep(6)  # Exercise actual 5s heartbeat expiry, not an admin-edited timestamp.
        processes.start_executor()
        processes.start_worker()
        wait(lambda: repo.get(run["run_id"])["status"] == terminal)
        assert (
            system[-1]
            .execute(
                "SELECT count(*) FROM agent_business.run_model_calls "
                "WHERE run_id=%s AND call_key=%s",
                (run["run_id"], row[0]),
            )
            .fetchone()[0]
            == 1
        )
        turns = repo.sessions.list_turns(run["conversation_id"])
        assert len(turns) == (2 if terminal == "succeeded" else 0)
        if terminal == "succeeded":
            assert turns[-1].metadata["citations"]
    finally:
        processes.close()


def test_real_redis_restart_and_worker_hard_stop_do_not_lose_outbox_or_duplicate_answer(
    system, tmp_path
):
    project = os.getenv("COMPOSE_PROJECT_NAME", "")
    if not project.startswith(("wms-p0-", "wms-quality-")):
        pytest.fail("Only this job's explicitly named disposable Redis may be restarted")
    repo, run, _ = accepted(system)
    processes = Processes(tmp_path)
    try:
        processes.start_executor()  # Dispatcher receives/acks; no worker yet.
        wait(
            lambda: (
                system[-1]
                .execute(
                    "SELECT dispatched FROM agent_business.run_outbox WHERE run_id=%s",
                    (run["run_id"],),
                )
                .fetchone()[0]
            )
        )
        subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                "data/p0-fixture/.env",
                "-f",
                "infra/p0/compose.yml",
                "restart",
                "redis",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        processes.start_worker()
        wait(lambda: repo.get(run["run_id"])["status"] == "succeeded")
        stop(processes.worker)
        processes.start_worker()  # Late/duplicate deliveries still cannot commit again.
        time.sleep(1)
        assert len(repo.sessions.list_turns(run["conversation_id"])) == 2
        assert repo.sessions.get_session(run["conversation_id"]).current_revision == 2
    finally:
        processes.close()
