"""Execute fixed release suites and publish counts, never private test output."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SUITES = {
    "v1": ("tests/e2e/test_recall_benchmark.py",),
    "agent_mvp": ("tests/e2e/test_agent_golden_workflows.py",),
    "product": (
        "tests/e2e/test_product_release.py",
        "tests/integration/test_workspace_workflow.py",
        "tests/integration/test_agent_workbench.py",
        "tests/unit/test_knowledge_catalog.py",
        "tests/unit/test_diagnostic_response.py",
        "tests/unit/test_feedback.py",
        "tests/unit/test_action_catalog.py",
    ),
}


def junit_counts(path: Path) -> dict[str, int]:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    return {
        "tests": len(cases),
        "failed": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }


def run_release(root: Path) -> dict[str, Any]:
    gates = []
    environment = dict(os.environ)
    # No inherited pytest selection or opt-in live provider flags in this offline gate.
    environment["PYTEST_ADDOPTS"] = ""
    environment["WMS_AGENT_LIVE"] = "0"
    environment["WMS_LLM_INTEGRATION"] = "0"
    for name, paths in SUITES.items():
        counts = {"tests": 0, "failed": 0, "errors": 0, "skipped": 0}
        status = "execution_error"
        with tempfile.TemporaryDirectory(prefix="wms-release-") as directory:
            report = Path(directory) / "results.xml"
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-p",
                        "no:cacheprovider",
                        "-q",
                        "--basetemp",
                        str(Path(directory) / "pytest"),
                        f"--junitxml={report}",
                        *paths,
                    ],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    timeout=180,
                    check=False,
                )
                counts = junit_counts(report)
                passed = (
                    result.returncode == 0
                    and counts["tests"] > 0
                    and not any(counts[key] for key in ("failed", "errors", "skipped"))
                )
                status = "passed" if passed else "failed"
            except (OSError, subprocess.TimeoutExpired, ET.ParseError):
                pass
        gates.append({"name": name, "status": status, **counts})
    return {
        "schema_version": 1,
        "evaluation": "executed_offline_tests",
        "passed": all(gate["status"] == "passed" for gate in gates),
        "gates": gates,
        "not_run": ["real_provider", "private_corpus", "browser_visual", "production_wms"],
    }
