"""Two real isolated browsers, Keycloak mail links and non-owner PG, no real model."""

import html
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg
import pytest
from playwright.sync_api import sync_playwright

from agents.workspace import Workspace

pytestmark = pytest.mark.skipif(
    os.getenv("WMS_P2_BROWSER") != "1",
    reason="P2 browser requires disposable identity/mail/PG and built React",
)
WEB = "http://127.0.0.1:5173/"


def navigate(page, url):
    try:
        page.goto(url)
    except Exception:
        raise AssertionError("Navigation failed at " + urlsplit(url).path) from None


@pytest.fixture(scope="module")
def browser():
    env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
    backend = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "scripts.p2_fixture_app:create_fixture_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8510",
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    frontend = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd="frontend",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if (
                    httpx.get(WEB, timeout=1).status_code == 200
                    and httpx.get("http://127.0.0.1:8510/v1/web-config", timeout=1).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise AssertionError("Disposable UI/API did not become ready")
        with sync_playwright() as playwright:
            instance = playwright.chromium.launch(headless=True)
            yield instance
            instance.close()
    finally:
        for process in (frontend, backend):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        frontend.wait(timeout=10)
        backend.wait(timeout=10)


def signin(page, name, password):
    navigate(page, WEB)
    page.get_by_role("button", name="登录工作台").click()
    page.locator('input[name="username"]').fill(name)
    page.locator('input[name="password"]').fill(password)
    page.locator("#kc-login").click()
    page.get_by_role("textbox", name="输入问题").wait_for(timeout=20000)


def provision(subject_user_id):
    workspace = Workspace(
        "workspace:p2", "P2 synthetic", ("fixture",), ("inbound",), ("DC01",), ("test",)
    )
    with psycopg.connect(os.environ["P0_POSTGRES_DSN"], autocommit=True) as admin:
        admin.execute(
            "INSERT INTO identity_business.workspaces VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (workspace.workspace_id, json.dumps(asdict(workspace))),
        )
        admin.execute(
            "INSERT INTO identity_business.memberships VALUES(%s,%s,'reviewer',true) "
            "ON CONFLICT DO NOTHING",
            (subject_user_id, workspace.workspace_id),
        )


def mail_link(recipient):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        messages = httpx.get("http://127.0.0.1:28025/api/v1/messages", timeout=3).json()["messages"]
        for message in messages:
            if any(to["Address"] == recipient for to in message.get("To", [])):
                data = httpx.get(
                    "http://127.0.0.1:28025/api/v1/message/" + message["ID"], timeout=3
                ).json()
                links = re.findall(r'href=["\']([^"\']+)', data.get("HTML", ""))
                link = next(
                    (
                        html.unescape(value)
                        for value in links
                        if "/login-actions/action-token" in value
                    ),
                    None,
                )
                if link:
                    return link
        time.sleep(0.2)
    raise AssertionError("Synthetic verification/reset mail not delivered")


def test_two_real_accounts_private_chat_profile_logout_and_reload(browser):
    contexts = [browser.new_context(viewport={"width": 1360, "height": 900}) for _ in range(2)]
    try:
        pages = [c.new_page() for c in contexts]
        auth = ["", ""]
        for index, page in enumerate(pages):

            def capture(request, index=index):
                if "/v1/me" in request.url:
                    auth[index] = request.headers.get("authorization", "")

            page.on("request", capture)
            signin(
                page,
                "user-a" if index == 0 else "user-b",
                os.environ["P0_A_PASSWORD" if index == 0 else "P0_B_PASSWORD"],
            )
            response = httpx.get(
                "http://127.0.0.1:8510/v1/me", headers={"Authorization": auth[index]}, timeout=5
            )
            assert response.status_code == 200
            provision(response.json()["user_id"])
            navigate(page, WEB)
            page.get_by_role("textbox", name="输入问题").wait_for(timeout=15000)
        a, b = pages
        a.get_by_role("textbox", name="输入问题").fill("What is SYN_MODE?")
        a.get_by_role("button", name="发送问题").click()
        a.get_by_text("SYN_MODE is optional.", exact=False).first.wait_for(timeout=15000)
        a.locator(".conversation-title").first.wait_for()
        cid = httpx.get(
            "http://127.0.0.1:8510/v1/conversations?workspace_id=workspace:p2",
            headers={"Authorization": auth[0]},
            timeout=5,
        ).json()[0]["session_id"]
        assert (
            httpx.get(
                "http://127.0.0.1:8510/v1/conversations/" + cid + "/workbench",
                headers={"Authorization": auth[1]},
                timeout=5,
            ).status_code
            == 404
        )
        b.get_by_role("button", name="刷新对话列表").click()
        assert not b.locator(".conversation-title").count()
        navigate(a, WEB)
        a.locator(".conversation-title").first.click()
        a.get_by_text("SYN_MODE is optional.", exact=False).first.wait_for(timeout=15000)
        a.get_by_role("button", name="工作区", exact=True).click()
        a.get_by_text("第 1 次回答", exact=False).wait_for()
        a.get_by_role("button", name="回到当前回答").click()
        reports = Path("data/p2-reports")
        reports.mkdir(parents=True, exist_ok=True)
        a.screenshot(path=str(reports / "workbench-desktop.png"))
        a.set_viewport_size({"width": 390, "height": 844})
        a.screenshot(path=str(reports / "workbench-mobile.png"))
        assert a.get_by_role("textbox", name="输入问题").is_visible()
        a.get_by_role("button", name="打开侧栏").click()
        a.get_by_role("button", name="我的账号", exact=False).click()
        a.get_by_role("button", name="退出当前账号").click()
        a.get_by_role("button", name="登录工作台").wait_for(timeout=15000)
        signin(a, "user-a", os.environ["P0_A_PASSWORD"])
        a.get_by_role("button", name="打开侧栏").click()
        a.locator(".conversation-title").first.click()
        a.get_by_text("SYN_MODE is optional.", exact=False).first.wait_for(timeout=15000)
    finally:
        for context in contexts:
            context.close()


def test_registration_mail_verification_and_reset_revoke_old_sessions(browser):
    context = browser.new_context()
    page = context.new_page()
    email = "p2-" + uuid.uuid4().hex + "@example.invalid"
    name = "p2-" + uuid.uuid4().hex
    password = uuid.uuid4().hex + "Aa9!"
    changed = uuid.uuid4().hex + "Bb9!"
    try:
        navigate(page, WEB)
        page.get_by_role("button", name="创建账号").click()
        for field, value in [
            ("username", name),
            ("firstName", "Synthetic"),
            ("lastName", "P2"),
            ("email", email),
        ]:
            page.locator('input[name="' + field + '"]').fill(value)
        assert not page.locator('input[name="password"]').count()
        page.locator('input[type="submit"],button[type="submit"]').first.click()
        link = mail_link(email)
        navigate(page, link)
        page.locator('input[name="password-new"]').fill(password)
        page.locator('input[name="password-confirm"]').fill(password)
        page.locator('input[type="submit"],button[type="submit"]').first.click()
        # The server-side password policy can revoke the setup session too;
        # confirm the account using a fresh normal login, never reuse old tokens.
        navigate(page, WEB)
        if page.get_by_role("button", name="登录工作台").is_visible():
            page.get_by_role("button", name="登录工作台").click()
            page.locator('input[name="username"]').fill(name)
            page.locator('input[name="password"]').fill(password)
            page.locator("#kc-login").click()
        page.get_by_role("textbox", name="输入问题").wait_for(timeout=20000)
        page.get_by_text("账号已就绪", exact=False).wait_for(timeout=15000)
        assert not page.get_by_role("button", name="发送问题").is_enabled()
        token = [""]
        page.on(
            "request",
            lambda request: (
                token.__setitem__(0, request.headers.get("authorization", ""))
                if "/v1/me" in request.url
                else None
            ),
        )
        page.get_by_role("button", name="我的账号", exact=False).click()
        page.get_by_text(email, exact=False).wait_for()
        with page.expect_response(
            lambda response: response.url.endswith("/v1/me") and response.status == 200
        ):
            page.evaluate('window.dispatchEvent(new Event("focus"))')
        old = token[0]
        reset_context = browser.new_context()
        reset_page = reset_context.new_page()
        navigate(reset_page, WEB)
        reset_page.get_by_role("button", name="登录工作台").click()
        reset_page.locator('a[href*="reset-credentials"]').click()
        reset_page.locator('input[name="username"]').fill(email)
        reset_page.locator('input[type="submit"],button[type="submit"]').first.click()
        reset_link = mail_link(email)
        navigate(reset_page, reset_link)
        reset_page.locator('input[name="password-new"]').fill(changed)
        reset_page.locator('input[name="password-confirm"]').fill(changed)
        reset_page.locator('input[type="submit"],button[type="submit"]').first.click()
        assert (
            httpx.get(
                "http://127.0.0.1:8510/v1/me", headers={"Authorization": old}, timeout=5
            ).status_code
            == 401
        )
        navigate(reset_page, reset_link)
        assert not reset_page.locator('input[name="password-new"]').count()
        reset_context.close()
    finally:
        context.close()


def test_unknown_account_recovery_is_uniform_and_expired_link_is_rejected(browser):
    contexts = []
    texts = []
    try:
        for email in ["user-a@example.invalid", "unknown-" + uuid.uuid4().hex + "@example.invalid"]:
            context = browser.new_context()
            contexts.append(context)
            page = context.new_page()
            navigate(page, WEB)
            page.get_by_role("button", name="登录工作台").click()
            page.locator('a[href*="reset-credentials"]').click()
            page.locator('input[name="username"]').fill(email)
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.locator('input[type="submit"],button[type="submit"]').first.click()
            texts.append(page.locator("body").inner_text())
        assert texts[0] == texts[1]
        link = mail_link("user-a@example.invalid")
        time.sleep(31)  # Real expiry in the disposable 30-second action-token policy.
        page = contexts[0].new_page()
        navigate(page, link)
        assert not page.locator('input[name="password-new"]').count()
    finally:
        for context in contexts:
            context.close()
