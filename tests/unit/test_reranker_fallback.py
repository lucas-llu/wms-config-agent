from __future__ import annotations

from typing import Any

import pytest

from core.query_engine import SafeReranker
from core.settings import RerankSettings
from core.types import RetrievalResult
from libs.reranker import BaseReranker, NoneReranker, RerankerFactory


def _candidates() -> list[RetrievalResult]:
    return [
        RetrievalResult(
            chunk_id="a",
            score=1.0,
            text="text",
            metadata={"source_path": "a.pdf"},
        )
    ]


class BrokenReranker(BaseReranker):
    def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        trace: Any | None = None,
    ) -> list[RetrievalResult]:
        del query, candidates, trace
        raise TimeoutError("model timeout")


def test_none_reranker_preserves_order() -> None:
    candidates = _candidates()

    outcome = SafeReranker(NoneReranker()).rerank("query", candidates)

    assert list(outcome.results) == candidates
    assert outcome.fallback_used is False


def test_broken_reranker_falls_back() -> None:
    candidates = _candidates()

    outcome = SafeReranker(BrokenReranker()).rerank("query", candidates)

    assert list(outcome.results) == candidates
    assert outcome.fallback_used is True
    assert outcome.failure == "TimeoutError: model timeout"


def test_reranker_factory_builds_none_and_rejects_unknown() -> None:
    assert isinstance(
        RerankerFactory.create(RerankSettings(backend="none", model=None, top_m=5)),
        NoneReranker,
    )

    with pytest.raises(ValueError, match="Unknown reranker backend"):
        RerankerFactory.create(RerankSettings(backend="missing", model=None, top_m=5))
