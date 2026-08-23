from __future__ import annotations

import pytest

from core.query_engine import QueryProcessor


def test_query_processor_infers_configuration_and_process_code() -> None:
    processed = QueryProcessor().process(
        "如何配置 SWL.I.11.01 的上架库位？",
        {"doc_type": "configuration"},
    )

    assert processed.filters == {
        "document_type": "configuration",
        "process_code": "SWL.I.11.01",
    }
    assert "putaway" in processed.retrieval_query
    assert "storage location" in processed.retrieval_query
    assert "configuration" in processed.keywords


def test_query_processor_infers_only_unambiguous_domain() -> None:
    inbound = QueryProcessor().process("inbound receiving appointment")
    cross_domain = QueryProcessor().process("从收货直接转到发货")

    assert inbound.filters["domain"] == "Inbound"
    assert "domain" not in cross_domain.filters


def test_query_processor_recognizes_configured_as_configuration_intent() -> None:
    processed = QueryProcessor().process("How is RF picking configured?")

    assert processed.filters["document_type"] == "configuration"


def test_query_processor_rejects_unsupported_filter() -> None:
    with pytest.raises(ValueError, match="Unsupported metadata filter"):
        QueryProcessor().process("putaway", {"customer": "secret"})


@pytest.mark.parametrize(
    ("query", "expected_terms"),
    [
        ("如何配置RF补货任务巡回和移动区域？", ("replenishment", "tour", "movement zone")),
        ("RF库存调整的原因在哪里配置？", ("inventory adjustment",)),
        ("增值服务工作单创建配置", ("value added service VAS", "work order creation")),
        ("RF cycle count settings", ("RF Based Cycle Count",)),
    ],
)
def test_query_processor_expands_benchmark_business_terms(
    query: str, expected_terms: tuple[str, ...]
) -> None:
    processed = QueryProcessor().process(query)

    assert all(term in processed.retrieval_query for term in expected_terms)
