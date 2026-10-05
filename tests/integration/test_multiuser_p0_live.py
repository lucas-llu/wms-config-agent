"""Real disposable services, never the user's corpus/session/credential stores."""

import asyncio
import base64
import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command
from playwright.sync_api import sync_playwright

from agents.runtime import build_runtime_probe_graph
from api.p0 import create_app
from multiuser.checkpoints import checkpoint_thread, open_postgres_checkpointer
from multiuser.identity import OIDCVerifier
from multiuser.probe_ledger import ProbeLedger
from workers.p0 import dispatch_pending, probe_queue, redis_client

pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P0_LIVE") != "1", reason="P0 requires disposable services"
)


@contextmanager
def callback_listener(state):
    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Authorization codes never enter access logs.

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path != "/callback" or parse_qs(parsed.query).get("state") != [state]:
                self.send_error(400)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"P0 callback")

    server = ThreadingHTTPServer(("127.0.0.1", 18080), Callback)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def login(username, password, *, include_tokens=False):
    issuer = os.environ["P0_OIDC_ISSUER"]
    state, verifier = uuid.uuid4().hex, uuid.uuid4().hex + uuid.uuid4().hex
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    redirect = "http://127.0.0.1:18080/callback"
    authorize = (
        issuer
        + "/protocol/openid-connect/auth?"
        + urlencode(
            {
                "client_id": "wms-p0-cli",
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": redirect,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    with callback_listener(state), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            failures, navigation = [], []

            def failed(request):
                url = urlsplit(request.url)
                failures.append(
                    {
                        "origin": url.scheme + "://" + url.netloc,
                        "path": url.path,
                        "error": request.failure,
                    }
                )

            def navigated(frame):
                url = urlsplit(frame.url)
                navigation.append({"origin": url.scheme + "://" + url.netloc, "path": url.path})

            page.on("requestfailed", failed)
            page.on("framenavigated", navigated)
            # A real loopback RP receives the IDP redirect. Browser route handlers
            # only intercept the first URL of a redirect chain, not this callback.
            callback_pattern = re.compile(r"^" + re.escape(redirect) + r"\?.*$")
            page.goto(authorize)
            page.locator('input[name="username"]').fill(username)
            page.locator('input[name="password"]').fill(password)
            page.locator('[name="login"]').click()
            try:
                page.wait_for_url(callback_pattern, timeout=20_000)
            except Exception as exc:
                raise AssertionError({"failures": failures, "navigation": navigation}) from exc
            location = urlsplit(page.url)
        finally:
            browser.close()
        assert location.netloc == "127.0.0.1:18080" and location.path == "/callback"
        params = parse_qs(location.query)
        assert params["state"] == [state]
        assert params.get("iss", [issuer]) == [issuer]
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        response = client.post(
            issuer + "/protocol/openid-connect/token",
            data={
                "client_id": "wms-p0-cli",
                "grant_type": "authorization_code",
                "code": params["code"][0],
                "redirect_uri": redirect,
                "code_verifier": verifier,
            },
        )
        assert response.status_code == 200
        return response.json() if include_tokens else response.json()["access_token"]


def test_real_keycloak_pkce_login_to_api_identity():
    verifier = OIDCVerifier(os.environ["P0_OIDC_ISSUER"], "wms-api", allow_local_http=True)
    with TestClient(create_app(verifier)) as client:
        identities = []
        for username, key in (("user-a", "P0_A_PASSWORD"), ("user-b", "P0_B_PASSWORD")):
            token = login(username, os.environ[key])
            result = client.get("/v1/me", headers={"Authorization": "Bearer " + token})
            assert result.status_code == 200
            identities.append(result.json()["identity_key"])
            assert result.json()["knowledge_access"] == []
        assert identities[0] != identities[1]
        assert client.get("/v1/me").status_code == 401


def test_real_postgres_checkpoint_pause_reopen_resume_and_identity_namespace():
    async def run():
        dsn = os.environ["P0_POSTGRES_DSN"]
        conversation = uuid.uuid4().hex
        a = {"configurable": {"thread_id": checkpoint_thread("owner-a", conversation)}}
        b = {"configurable": {"thread_id": checkpoint_thread("owner-b", conversation)}}
        async with open_postgres_checkpointer(dsn, setup=True) as saver:
            graph = build_runtime_probe_graph(saver)
            await graph.ainvoke({"subject": "synthetic-user-a"}, a)
            assert (await graph.aget_state(a)).next == ("approval",)
            assert not (await graph.aget_state(b)).values
        async with open_postgres_checkpointer(dsn) as saver:
            graph = build_runtime_probe_graph(saver)
            await graph.ainvoke(Command(resume={"approved": True}), a)
            assert (await graph.aget_state(a)).values["result"] == "approved"
            assert not (await graph.aget_state(a)).next

    asyncio.run(run())


@pytest.fixture
def ledger():
    value = ProbeLedger(os.environ["P0_POSTGRES_DSN"])
    value.setup()
    yield value
    value.close()


def worker(*, lease=45):
    env = {**os.environ, "P0_LEASE_SECONDS": str(lease), "PYTHONPATH": str(Path("src").resolve())}
    return subprocess.Popen(
        [sys.executable, "-m", "workers.p0", "--burst"],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )


def test_real_rq_outbox_worker_reopen_duplicate_and_fencing(ledger):
    client = redis_client()
    queue = probe_queue(client)
    try:
        owner, conversation, key = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        run = ledger.create(owner, conversation, key, "synthetic")
        assert ledger.create(owner, conversation, key, "synthetic") == run
        with pytest.raises(ValueError):
            ledger.create(owner, conversation, key, "different")

        class Unavailable:
            def enqueue(self, *args, **kwargs):
                raise RuntimeError("synthetic transport outage")

        with pytest.raises(RuntimeError):
            dispatch_pending(ledger, Unavailable())
        assert any(r["run_id"] == run for r in ledger.pending())

        class AckFailure:
            def pending(self):
                return ledger.pending()

            def dispatched(self, *args):
                raise RuntimeError("synthetic outbox acknowledgement failure")

        with pytest.raises(RuntimeError):
            dispatch_pending(AckFailure(), queue)
        # Same delivery already in Redis: unique enqueue + validated acknowledgement.
        dispatch_pending(ledger, queue)
        process = worker()
        assert process.wait(timeout=30) == 0
        assert ledger.get(run)["status"] == "succeeded" and ledger.result_count(run) == 1
        # A separate actual delivery executes, but cannot commit a duplicate result.
        from workers.p0 import execute_probe

        queue.enqueue(execute_probe, run, job_id=uuid.uuid4().hex)
        assert worker().wait(timeout=30) == 0
        assert ledger.result_count(run) == 1
        crash = ledger.create(owner, uuid.uuid4().hex, uuid.uuid4().hex, "crash", delay=15)
        dispatch_pending(ledger, queue)
        process = worker(lease=2)
        try:
            deadline = time.monotonic() + 15
            while ledger.get(crash)["status"] != "running" and time.monotonic() < deadline:
                time.sleep(0.05)
            old_epoch = ledger.get(crash)["epoch"]
            assert old_epoch > 0
            os.killpg(process.pid, signal.SIGKILL)  # Only the process group created above.
            process.wait(timeout=10)
            time.sleep(2.2)
            assert ledger.recover_expired() == 1
            assert not ledger.finish(crash, old_epoch)
            dispatch_pending(ledger, queue)
            assert worker().wait(timeout=30) == 0
            assert ledger.get(crash)["epoch"] > old_epoch
            assert ledger.get(crash)["status"] == "succeeded"
            assert ledger.result_count(crash) == 1
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
    finally:
        client.close()
        client.connection_pool.disconnect()


def test_real_concurrency_baseline(ledger):
    import math
    import platform
    import statistics
    from concurrent.futures import ThreadPoolExecutor

    measurements = []
    for users in (1, 2, 5, 10, 20):

        def submit(index):
            start = time.perf_counter()
            ledger.create(uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex, "synthetic")
            return time.perf_counter() - start

        with ThreadPoolExecutor(max_workers=users) as executor:
            times = list(executor.map(submit, range(users * 5)))
        measurements.append(
            {
                "users": users,
                "accepted": len(times),
                "max_seconds": max(times),
                "median_seconds": statistics.median(times),
                "sample_p95_seconds": sorted(times)[math.ceil(len(times) * 0.95) - 1],
            }
        )
    target = Path(os.getenv("P0_REPORT_DIR", "data/p0-reports"))
    target.mkdir(parents=True, exist_ok=True)
    import json

    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "database_pool_max": 4,
        "layer": "PostgreSQL durable-run admission only",
        "measurements": measurements,
    }
    (target / "admission-baseline.json").write_text(json.dumps(report, indent=2))
