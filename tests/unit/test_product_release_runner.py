from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from observability.evaluation import product_release


@pytest.mark.parametrize(
    "xml,code,passed",
    [
        ("<testsuites><testsuite><testcase/></testsuite></testsuites>", 0, True),
        ("<testsuite><testcase><failure>private</failure></testcase></testsuite>", 1, False),
        ("<testsuite><testcase><skipped/></testcase></testsuite>", 0, False),
        ("<testsuite><testcase><error/></testcase></testsuite>", 0, False),
        ("<testsuite/>", 0, False),
        ("bad xml", 0, False),
        ("<testsuite><testcase/></testsuite>", 5, False),
    ],
)
def test_gate_uses_actual_junit_and_exit_status(tmp_path, monkeypatch, xml, code, passed):
    monkeypatch.setattr(product_release, "SUITES", {"test": ("tests/safe.py",)})
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k skip_everything")

    def run(command, **kwargs):
        assert kwargs["env"]["PYTEST_ADDOPTS"] == ""
        assert kwargs["env"]["WMS_AGENT_LIVE"] == "0"
        path = next(item.split("=", 1)[1] for item in command if item.startswith("--junitxml="))
        Path(path).write_text(xml, encoding="utf-8")
        return SimpleNamespace(returncode=code, stdout="private-canary", stderr="secret")

    monkeypatch.setattr(product_release.subprocess, "run", run)
    report = product_release.run_release(tmp_path)
    assert report["passed"] is passed
    assert "private" not in json.dumps(report["gates"])
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize(
    "exception", [OSError("private"), subprocess.TimeoutExpired("private", 180)]
)
def test_execution_errors_fail_closed(tmp_path, monkeypatch, exception):
    monkeypatch.setattr(product_release, "SUITES", {"test": ("tests/safe.py",)})

    def run(*args, **kwargs):
        raise exception

    monkeypatch.setattr(product_release.subprocess, "run", run)
    report = product_release.run_release(tmp_path)
    assert not report["passed"]
    assert report["gates"][0]["status"] == "execution_error"


def test_missing_junit_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(product_release, "SUITES", {"test": ("tests/safe.py",)})
    monkeypatch.setattr(
        product_release.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)
    )
    assert not product_release.run_release(tmp_path)["passed"]
