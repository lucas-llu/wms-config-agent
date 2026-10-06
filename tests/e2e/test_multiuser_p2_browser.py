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
    execution = worker = None
    if os.getenv("WMS_P3_LIVE") == "1":
        env.update(
            WMS_EXECUTION_URL="http://127.0.0.1:8531",
            WMS_EXECUTION_TOKEN=os.environ["P3_EXECUTION_TOKEN"],
            WMS_REDIS_URL=os.environ["P0_REDIS_URL"],
        )
        execution = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.p3_fixture_app:create_fixture_executor",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8531",
                "--no-access-log",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        worker = subprocess.Popen(
            [sys.executable, "-m", "workers.runs"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
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
        for process in (frontend, backend, worker, execution):
            if process is None:
                continue
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        frontend.wait(timeout=10)
        backend.wait(timeout=10)
        for process in (worker, execution):
            if process:
                process.wait(timeout=10)


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


def register_verified(page):
    name = "p2-" + uuid.uuid4().hex
    email = name + "@example.invalid"
    password = uuid.uuid4().hex + "Aa9!"
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
    navigate(page, mail_link(email))
    page.locator('input[name="password-new"]').fill(password)
    page.locator('input[name="password-confirm"]').fill(password)
    page.locator('input[type="submit"],button[type="submit"]').first.click()
    navigate(page, WEB)
    if page.get_by_role("button", name="登录工作台").is_visible():
        signin(page, name, password)
    page.get_by_text("账号已就绪", exact=False).wait_for(timeout=20000)
    assert not page.get_by_role("button", name="发送问题").is_enabled()
    return name, email, password


def test_two_real_accounts_private_chat_profile_logout_and_reload(browser):
    contexts = [browser.new_context(viewport={"width": 1360, "height": 900}) for _ in range(2)]
    try:
        pages = [c.new_page() for c in contexts]
        auth = ["", ""]
        registered = []
        for index, page in enumerate(pages):

            def capture(request, index=index):
                if "/v1/me" in request.url:
                    auth[index] = request.headers.get("authorization", "")

            page.on("request", capture)
            name, email, password = register_verified(page)
            registered.append((name, password))
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
        b.get_by_role("textbox", name="输入问题").fill("SYN_MODE 是什么？")
        b.get_by_role("button", name="发送问题").click()
        b.get_by_text("SYN_MODE 为可选项。", exact=False).first.wait_for(timeout=15000)
        own_b = httpx.get(
            "http://127.0.0.1:8510/v1/conversations?workspace_id=workspace:p2",
            headers={"Authorization": auth[1]},
            timeout=5,
        ).json()[0]["session_id"]
        assert (
            httpx.get(
                "http://127.0.0.1:8510/v1/conversations/" + own_b + "/workbench",
                headers={"Authorization": auth[0]},
                timeout=5,
            ).status_code
            == 404
        )
        navigate(a, WEB)
        a.locator(".conversation-title").first.click()
        a.get_by_text("SYN_MODE is optional.", exact=False).first.wait_for(timeout=15000)
        b.get_by_role("button", name="我的账号", exact=False).click()
        b.get_by_role("button", name="退出当前账号").click()
        b.get_by_role("button", name="登录工作台").wait_for(timeout=15000)
        signin(b, *registered[1])
        b.locator(".conversation-title").first.click()
        b.get_by_text("SYN_MODE 为可选项。", exact=False).first.wait_for(timeout=15000)
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
        signin(a, *registered[0])
        a.get_by_role("button", name="打开侧栏").click()
        a.locator(".conversation-title").first.click()
        a.get_by_text("SYN_MODE is optional.", exact=False).first.wait_for(timeout=15000)
    finally:
        for context in contexts:
            context.close()


def test_registration_mail_verification_and_reset_revoke_old_sessions(browser):
    context = browser.new_context()
    page = context.new_page()
    changed = uuid.uuid4().hex + "Bb9!"
    refresh = [""]

    def capture_tokens(response):
        if response.url.endswith("/protocol/openid-connect/token") and response.status == 200:
            refresh[0] = response.json().get("refresh_token", "")

    page.on("response", capture_tokens)
    try:
        name, email, password = register_verified(page)
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
        old_refresh = refresh[0]
        assert old_refresh
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
        assert (
            httpx.post(
                os.environ["P0_OIDC_ISSUER"] + "/protocol/openid-connect/token",
                data={
                    "client_id": "wms-workbench",
                    "grant_type": "refresh_token",
                    "refresh_token": old_refresh,
                },
                timeout=5,
            ).status_code
            == 400
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


def test_real_device_center_revoke_others_preserves_current_browser(browser):
    contexts = [browser.new_context(), browser.new_context()]
    auth = ["", ""]
    try:
        pages = [c.new_page() for c in contexts]
        for index, page in enumerate(pages):

            def capture(request, index=index):
                if request.url.endswith("/v1/me"):
                    auth[index] = request.headers.get("authorization", "")

            page.on("request", capture)
        name, email, password = register_verified(pages[0])
        signin(pages[1], name, password)
        pages[0].get_by_role("button", name="我的账号", exact=False).click()
        pages[0].get_by_role("button", name="登录设备", exact=True).click()
        pages[0].locator(".device-row").first.wait_for(timeout=15000)
        assert pages[0].locator(".device-row").count() >= 2
        pages[0].get_by_role("button", name="退出其他设备", exact=True).click()
        with pages[0].expect_response(
            lambda response: (
                response.url.endswith("/v1/me/logout-others") and response.status == 200
            )
        ):
            pages[0].get_by_role("dialog").get_by_role("button", name="确认", exact=True).click()
        assert (
            httpx.get(
                "http://127.0.0.1:8510/v1/me", headers={"Authorization": auth[0]}, timeout=5
            ).status_code
            == 200
        )
        assert (
            httpx.get(
                "http://127.0.0.1:8510/v1/me", headers={"Authorization": auth[1]}, timeout=5
            ).status_code
            == 401
        )
        pages[1].evaluate('window.dispatchEvent(new Event("focus"))')
        pages[1].get_by_text("需要重新确认登录与权限").wait_for(timeout=15000)
    finally:
        for context in contexts:
            context.close()


@pytest.mark.skipif(os.getenv("WMS_P3_LIVE") != "1", reason="P3 browser requires real RQ execution")
def test_p3_refresh_and_second_tab_recover_the_same_accepted_run(browser):
    context = browser.new_context(viewport={"width": 1360, "height": 900})
    auth = [""]
    posts = []
    try:
        page = context.new_page()

        def capture(request):
            if request.url.endswith("/v1/me"):
                auth[0] = request.headers.get("authorization", "")
            if request.method == "POST" and "/v1/conversations" in request.url:
                posts.append(request.url)

        page.on("request", capture)
        register_verified(page)
        profile = httpx.get(
            "http://127.0.0.1:8510/v1/me", headers={"Authorization": auth[0]}, timeout=5
        ).json()
        provision(profile["user_id"])
        navigate(page, WEB)
        question = "SYN_MODE 是什么？"
        page.get_by_role("textbox", name="输入问题").fill(question)
        with page.expect_response(
            lambda r: r.url.endswith("/v1/conversations") and r.status == 202
        ) as accepted:
            page.get_by_role("button", name="发送问题").click()
        run = accepted.value.json()
        page.reload()
        page.get_by_text("SYN_MODE 为可选项。", exact=False).first.wait_for(timeout=20000)
        assert len(posts) == 1
        second = context.new_page()
        navigate(second, WEB)
        second.locator(".conversation-title").first.click()
        second.get_by_text("SYN_MODE 为可选项。", exact=False).first.wait_for(timeout=15000)
        data = httpx.get(
            "http://127.0.0.1:8510/v1/conversations/" + run["conversation_id"] + "/workbench",
            headers={"Authorization": auth[0]},
            timeout=5,
        ).json()
        assert [turn["role"] for turn in data["turns"]] == ["user", "assistant"]
        assert data["session"]["current_revision"] == 2
        assert len(posts) == 1
    finally:
        context.close()
