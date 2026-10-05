from __future__ import annotations

import json
from dataclasses import replace

from observability.evaluation import AgentEvaluationRunner, AgentGoldenDataset, AgentScenarioResult


def _results():
    return [
        AgentScenarioResult(**item)
        for item in json.loads(
            open("tests/fixtures/agent_golden_results.json", encoding="utf-8").read()
        )
    ]


def test_agent_golden_dataset_and_metrics_pass_every_release_threshold() -> None:
    dataset = AgentGoldenDataset.load("tests/fixtures/agent_golden_scenarios.json")

    report = AgentEvaluationRunner().run(dataset, _results())

    assert report.scenario_count == 6
    assert report.passed is True
    assert all(report.threshold_results.values())
    assert len(dataset.fingerprint) == 64


def test_agent_release_fails_for_gap_bypass_or_unauthorized_tool() -> None:
    dataset = AgentGoldenDataset.load("tests/fixtures/agent_golden_scenarios.json")
    results = _results()
    results[2] = replace(results[2], evidence_gap_blocked=False, unauthorized_tool_calls=1)

    report = AgentEvaluationRunner().run(dataset, results)

    assert report.passed is False
    assert report.failed_scenarios == ("missing-evidence",)
    assert report.threshold_results["evidence_gap_blocking_min"] is False
    assert report.threshold_results["unauthorized_tool_calls_max"] is False
