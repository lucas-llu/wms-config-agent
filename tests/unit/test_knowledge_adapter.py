from __future__ import annotations

import pytest

from agents.tools import KnowledgeAdapter, build_scope_filters
from core.query_engine import QueryProcessor, SafeReranker, SearchOutcome
from core.response import ResponseBuilder
from core.types import RetrievalResult
from libs.reranker import NoneReranker


class FakeHybridSearch:
    def __init__(self, outcome: SearchOutcome) -> None:
        self.outcome = outcome
        self.calls = []

    def search_with_details(self, query, top_k, filters, trace=None):
        self.calls.append((query, top_k, filters, trace))
        return self.outcome


def _outcome(
    *,
    sufficient: bool = True,
    failures=None,
    source_path: str = "D:/private/manuals/capacity.pdf",
) -> SearchOutcome:
    processed = QueryProcessor().process(
        "appointment capacity configuration",
        {"version": "2024.1", "module": "appointment"},
    )
    result = RetrievalResult(
        chunk_id="chunk:capacity",
        score=0.91,
        text="Appointment capacity is configured for the receiving schedule.",
        metadata={
            "source_path": source_path,
            "title": "Appointment Capacity",
            "page_start": 4,
            "page_end": 5,
            "version": "2024.1",
            "module": "appointment",
            "collection": "wms",
        },
    )
    results = (result,) if sufficient else ()
    return SearchOutcome(
        processed_query=processed,
        dense_results=results,
        sparse_results=results,
        fused_results=results,
        results=results,
        failures=failures or {},
        evidence_sufficient=sufficient,
    )


def _adapter(outcome: SearchOutcome) -> tuple[KnowledgeAdapter, FakeHybridSearch]:
    search = FakeHybridSearch(outcome)
    return (
        KnowledgeAdapter(
            search,  # type: ignore[arg-type]
            SafeReranker(NoneReranker()),
            ResponseBuilder(),
        ),
        search,
    )


def test_scope_filters_map_confirmed_context_to_v1_metadata_names() -> None:
    filters = build_scope_filters(
        {
            "product_version": "2024.1",
            "site": "DC01",
            "environment": "test",
        },
        module="appointment",
    )

    assert filters == {
        "environment": "test",
        "module": "appointment",
        "site": "DC01",
        "version": "2024.1",
    }


def test_adapter_reuses_v1_stack_and_maps_citations_to_stable_evidence() -> None:
    adapter, search = _adapter(_outcome(failures={"dense": "temporary failure"}))
    filters = {"module": "appointment", "version": "2024.1"}

    first = adapter.search("appointment capacity", filters=filters)
    second = adapter.search("appointment capacity", filters=filters)

    assert first.evidence_sufficient is True
    assert first.evidence[0].evidence_id == second.evidence[0].evidence_id
    assert first.evidence[0].source == "capacity.pdf"
    assert first.evidence[0].product_version == "2024.1"
    assert first.failures == ("dense:temporary failure",)
    assert search.calls[0][2] == filters


def test_insufficient_v1_outcome_never_creates_evidence() -> None:
    adapter, _search = _adapter(_outcome(sufficient=False))

    result = adapter.search("unknown configuration", filters={"module": "inbound"})

    assert result.evidence_sufficient is False
    assert result.evidence == ()


@pytest.mark.parametrize(
    ("source_path", "expected"),
    [
        ("D:/private/manuals/capacity.pdf", "capacity.pdf"),
        (r"D:\private\manuals\capacity.pdf", "capacity.pdf"),
        ("/private/manuals/capacity.pdf", "capacity.pdf"),
        (r"\\server\share\capacity.pdf", "capacity.pdf"),
        (r"manuals\capacity.pdf", "manuals/capacity.pdf"),
    ],
)
def test_evidence_source_sanitization_is_independent_of_host_os(
    source_path: str, expected: str
) -> None:
    adapter, _search = _adapter(_outcome(source_path=source_path))

    result = adapter.search("appointment capacity", filters={"module": "appointment"})

    assert result.evidence[0].source == expected
