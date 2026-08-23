from __future__ import annotations

import json

import pytest

from observability.evaluation import BaselineComparator, BaselineComparisonError


def _report(
    *,
    hit_at_3: float,
    first_passed: bool,
    second_passed: bool,
    first_rank: int | None = None,
) -> dict:
    return {
        "dataset_fingerprint": "same-dataset",
        "metrics": {
            "hit_at_3": hit_at_3,
            "hit_at_5": 1.0,
            "mrr_at_5": hit_at_3,
            "evidence_accuracy": hit_at_3,
            "refusal_accuracy": 1.0,
        },
        "cases": [
            {
                "case_id": "first",
                "passed": first_passed,
                "first_relevant_rank": first_rank,
            },
            {
                "case_id": "second",
                "passed": second_passed,
                "first_relevant_rank": 1,
            },
        ],
    }


def test_comparator_reports_fixed_cases_and_metric_improvements() -> None:
    baseline = _report(
        hit_at_3=0.5, first_passed=False, second_passed=True, first_rank=3
    )
    candidate = _report(
        hit_at_3=1.0, first_passed=True, second_passed=True, first_rank=1
    )

    comparison = BaselineComparator().compare(baseline, candidate)

    assert comparison.passed is True
    assert comparison.fixed_cases == ("first",)
    assert comparison.improvements == ("hit_at_3", "mrr_at_5", "evidence_accuracy")
    assert comparison.rank_changes == {"first": 2}


def test_comparator_reports_regressions_and_rejects_different_datasets() -> None:
    baseline = _report(hit_at_3=1.0, first_passed=True, second_passed=True)
    candidate = _report(hit_at_3=0.5, first_passed=True, second_passed=False)

    comparison = BaselineComparator().compare(baseline, candidate)
    assert comparison.passed is False
    assert comparison.new_failures == ("second",)
    assert "hit_at_3" in comparison.regressions

    candidate["dataset_fingerprint"] = "different"
    with pytest.raises(BaselineComparisonError, match="fingerprints"):
        BaselineComparator().compare(baseline, candidate)


def test_comparator_loads_json_and_rejects_case_mismatch(tmp_path) -> None:
    baseline = _report(hit_at_3=1.0, first_passed=True, second_passed=True)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")

    assert BaselineComparator.load(path)["dataset_fingerprint"] == "same-dataset"
    candidate = _report(hit_at_3=1.0, first_passed=True, second_passed=True)
    candidate["cases"].pop()
    with pytest.raises(BaselineComparisonError, match="case IDs"):
        BaselineComparator().compare(baseline, candidate)
    with pytest.raises(BaselineComparisonError, match="does not exist"):
        BaselineComparator.load(tmp_path / "missing.json")
