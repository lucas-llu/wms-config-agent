from __future__ import annotations

import pytest

from core.query_engine import ReciprocalRankFusion
from core.types import RetrievalResult


def _result(chunk_id: str, score: float, source: str) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=chunk_id,
        score=score,
        text=chunk_id,
        metadata={"source_path": f"{chunk_id}.pdf"},
        retrieval_sources=(source,),
        source_scores={source: score},
    )


def test_rrf_rewards_results_found_by_both_retrievers() -> None:
    fusion = ReciprocalRankFusion(k=60)
    dense = [_result("dense-only", 0.9, "dense"), _result("shared", 0.8, "dense")]
    sparse = [_result("shared", 8.0, "sparse"), _result("sparse-only", 7.0, "sparse")]

    results = fusion.fuse({"dense": dense, "sparse": sparse})

    assert results[0].chunk_id == "shared"
    assert results[0].retrieval_sources == ("dense", "sparse")
    assert results[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert results[0].source_ranks == {"dense": 2, "sparse": 1}


def test_rrf_tie_breaks_by_chunk_id() -> None:
    fusion = ReciprocalRankFusion(k=60)

    results = fusion.fuse(
        {
            "dense": [_result("b", 1.0, "dense")],
            "sparse": [_result("a", 1.0, "sparse")],
        }
    )

    assert [result.chunk_id for result in results] == ["a", "b"]
