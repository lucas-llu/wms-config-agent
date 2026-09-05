"""Generate a privacy-safe Agent golden-scenario release report."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from observability.evaluation import AgentEvaluationRunner, AgentGoldenDataset, AgentScenarioResult


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("tests/fixtures/agent_golden_scenarios.json")
    )
    parser.add_argument(
        "--results", type=Path, default=Path("tests/fixtures/agent_golden_results.json")
    )
    parser.add_argument("--output", type=Path, default=Path("data/evaluation/agent-release.json"))
    parser.add_argument("--enforce-thresholds", action="store_true")
    args = parser.parse_args()
    dataset = AgentGoldenDataset.load(args.dataset)
    raw_results = json.loads(args.results.read_text(encoding="utf-8"))
    results = [AgentScenarioResult(**item) for item in raw_results]
    report = AgentEvaluationRunner().run(dataset, results)
    payload = report.to_dict()
    payload["run_metadata"] = {"git_revision": _git_revision(), "provider": "deterministic-fake"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.enforce_thresholds and not report.passed:
        raise SystemExit(1)


def _git_revision() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


if __name__ == "__main__":
    main()
