from __future__ import annotations

import json

import pytest

from observability.evaluation import BenchmarkDataset, BenchmarkValidationError


def _dataset() -> dict:
    return {
        "schema_version": 1,
        "name": "test-v1",
        "description": "Sanitized test benchmark.",
        "thresholds": {"hit_at_3_min": 0.8, "p95_latency_ms_max": 2000},
        "test_cases": [
            {
                "id": "positive-1",
                "category": "semantic",
                "query": "How is putaway configured?",
                "filters": {"document_type": "configuration"},
                "expected": {
                    "chunk_ids": ["putaway-config"],
                    "process_codes": ["PROCESS-1"],
                    "sources": ["Inbound/putaway.pdf"],
                    "text_contains": ["storage location"],
                },
            },
            {
                "id": "negative-1",
                "category": "refusal",
                "query": "Unsupported quantum inventory feature",
                "expected": {"should_refuse": True},
            },
        ],
    }


def test_dataset_loads_and_has_stable_fingerprint(tmp_path) -> None:
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(_dataset()), encoding="utf-8")

    first = BenchmarkDataset.load(path)
    second = BenchmarkDataset.load(path)

    assert first.name == "test-v1"
    assert len(first.test_cases) == 2
    assert first.test_cases[0].expected.chunk_ids == ("putaway-config",)
    assert first.test_cases[0].expected.text_contains == ("storage location",)
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_committed_golden_set_conforms_to_benchmark_schema() -> None:
    dataset = BenchmarkDataset.load("tests/fixtures/golden_test_set.json")

    assert dataset.name == "public-sanitized-smoke-v1"
    assert len(dataset.test_cases) == 4


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload["test_cases"][0]["expected"].update(
                {"sources": ["C:\\private\\document.pdf"]}
            ),
            "relative paths",
        ),
        (
            lambda payload: payload["test_cases"].append(payload["test_cases"][0]),
            "unique",
        ),
        (
            lambda payload: payload["test_cases"][0].update({"expected": {}}),
            "relevance label",
        ),
    ],
)
def test_dataset_rejects_unreliable_or_private_ground_truth(tmp_path, mutation, message) -> None:
    payload = _dataset()
    mutation(payload)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BenchmarkValidationError, match=message):
        BenchmarkDataset.load(path)
