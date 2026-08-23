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


def test_query_processor_rejects_unsupported_filter() -> None:
    with pytest.raises(ValueError, match="Unsupported metadata filter"):
        QueryProcessor().process("putaway", {"customer": "secret"})
