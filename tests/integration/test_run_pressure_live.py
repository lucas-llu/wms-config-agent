"""Synthetic 1/2/5/10/20 execution samples, not production users/provider p95."""

import json
import os
import platform
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_run_execution_live import executor
from test_run_execution_live import p1_system as base_system
from test_run_execution_live import system as system_fixture
from test_run_execution_live import tokens as tokens_fixture

from multiuser.access import UserContext
from multiuser.queued_application import start_run
from multiuser.run_repository import RunRepository
from multiuser.runs import RunLimits
from scripts.p2_fixture_app import SyntheticModel

p1_system = base_system
system = system_fixture
tokens = tokens_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P3_LIVE") != "1", reason="P3 synthetic pressure needs isolated PG"
)


@pytest.mark.parametrize("clients", [1, 2, 5, 10, 20])
def test_synthetic_execution_peak_is_bounded_and_every_private_run_commits_once(system, clients):
    model = SyntheticModel()
    lock = threading.Lock()
    counters = {"active": 0, "peak": 0}

    class MeasuredModel:
        def chat(self, messages, trace=None):
            with lock:
                counters["active"] += 1
                counters["peak"] = max(counters["peak"], counters["active"])
            try:
                time.sleep(0.01)
                return model.chat(messages, trace)
            finally:
                with lock:
                    counters["active"] -= 1

    contexts = []
    for _index in range(clients):
        uid = uuid.uuid4()
        context = UserContext(
            str(uid),
            system[2][0].issuer,
            "synthetic-load:" + uid.hex,
            "synthetic-load:" + uid.hex,
            int(time.time()) - 1,
            int(time.time()) + 3600,
        )
        system[-1].execute(
            "INSERT INTO identity_business.users(user_id,identity_issuer,identity_subject) "
            "VALUES(%s,%s,%s)",
            (uid, context.issuer, context.subject),
        )
        system[-1].execute(
            "INSERT INTO identity_business.memberships VALUES(%s,%s,'member',true)",
            (uid, system[3].workspace_id),
        )
        contexts.append(context)
    start = time.perf_counter()
    runs = [
        start_run(
            system[5],
            context,
            goal="SYN_MODE synthetic sample " + str(index),
            workspace_id=system[3].workspace_id,
            answer_strategy="standard",
            idempotency_key=uuid.uuid4().hex,
            limits=RunLimits(),
        )
        for index, context in enumerate(contexts)
    ]
    with executor(system, model=MeasuredModel()) as service:
        # Explicit synthetic authority injection ONLY here, never a public app or real SID claim.
        service.authority = Mock()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda run: service.execute(run["run_id"]), runs))
        assert all(r.get("committed") for r in results), results
        assert 0 < counters["peak"] <= service.governor.limits.inflight
    elapsed = time.perf_counter() - start
    for context, run in zip(contexts, runs, strict=True):
        repo = RunRepository(system[4], context, system[3].workspace_id)
        assert len(repo.sessions.list_turns(run["conversation_id"])) == 2
        assert repo.sessions.get_session(run["conversation_id"]).current_revision == 2
    report = {
        "scope": "synthetic graph/PG; no real IDP or supplier pressure",
        "clients": clients,
        "completed": clients,
        "elapsed_seconds": round(elapsed, 3),
        "model_peak": counters["peak"],
        "model_cap": 2,
        "executor_threads": 4,
        "business_pool": 2,
        "control_pool": 4,
        "checkpoint_pool_per_execution": 4,
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
    }
    directory = Path("data/p3-reports")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"synthetic-{clients}.json").write_text(json.dumps(report), encoding="utf-8")
