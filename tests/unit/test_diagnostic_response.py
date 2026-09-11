from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator

from core.query_engine import ProcessedQuery, SearchOutcome
from core.response import ResponseBuilder
from core.response.diagnostic_response import diagnostic_payload, diagnostic_schema
from core.types import RetrievalResult
from mcp_server.tool_registry import ToolInputError
from mcp_server.tools.query_knowledge_hub import QueryKnowledgeHubTool


def outcome(
    text="Check role permissions and vehicle equipment rules in the test environment.",
    sufficient=True,
):
    result = RetrievalResult(
        chunk_id="public-1",
        score=0.1,
        text=text,
        metadata={"title": "RF guide", "source_relative_path": "RF/guide.pdf", "page_start": 3},
        retrieval_sources=("dense",),
    )
    query = ProcessedQuery(
        original_query="Set Down missing",
        normalized_query="Set Down missing",
        retrieval_query="Set Down missing",
        keywords=(),
        filters={},
        expansions=(),
        specific_terms=(),
    )
    return SearchOutcome(
        processed_query=query,
        results=(result,),
        dense_results=(result,),
        sparse_results=(),
        fused_results=(result,),
        failures={},
        evidence_sufficient=sufficient,
    )


def test_sections_preserve_exact_evidence_and_do_not_confirm_root_cause():
    response = ResponseBuilder().build(outcome())
    payload = diagnostic_payload(response)
    report = payload["troubleshooting"]
    assert not report["root_cause_confirmed"]
    assert len(report["sections"]) == 6
    assert report["sections"][0]["status"] == "evidence_gap"
    for section in report["sections"][1:]:
        assert section["status"] == "document_evidence"
        assert section["evidence"] == [{"text": response.citations[0].excerpt, "citation_ids": [1]}]
    assert "第 3 页" in payload["markdown"]
    Draft202012Validator.check_schema(diagnostic_schema())
    Draft202012Validator(diagnostic_schema()).validate(report)


@pytest.mark.parametrize(
    "text",
    ["Unrelated inventory text", "", "Roles may not be the cause; verify actual permissions."],
)
def test_no_generated_instructions_or_paraphrased_claims(text):
    response = ResponseBuilder().build(outcome(text))
    report = diagnostic_payload(response)["troubleshooting"]
    for section in report["sections"]:
        for evidence in section["evidence"]:
            assert evidence["text"] == response.citations[0].excerpt
        if not section["evidence"]:
            assert section["status"] == "evidence_gap"
            assert section["gap"]


def test_insufficient_evidence_never_promotes_low_confidence_results():
    payload = diagnostic_payload(ResponseBuilder().build(outcome(sufficient=False)))
    assert payload["status"] == "insufficient_evidence"
    assert not payload["citations"]
    assert all(not section["evidence"] for section in payload["troubleshooting"]["sections"])


def tool():
    search = Mock()
    search.search_with_details.return_value = outcome()
    reranker = Mock()
    reranker.rerank.return_value = SimpleNamespace(results=outcome().results, failure=None)
    assembler = Mock()
    assembler.assemble.return_value = []
    return QueryKnowledgeHubTool(search, reranker, ResponseBuilder(), assembler)


def test_mcp_mode_is_opt_in_and_preserves_filters_and_schema():
    query_tool = tool()
    default = query_tool.call({"query": "RF"})
    assert "troubleshooting" not in default["structuredContent"]
    result = query_tool.call(
        {"query": "RF", "collection": "allowed", "response_format": "troubleshooting"}
    )
    assert query_tool.hybrid_search.search_with_details.call_args.args[2] == {
        "collection": "allowed"
    }
    assert result["content"][0]["text"] == result["structuredContent"]["markdown"]
    Draft202012Validator(query_tool.definition().output_schema).validate(
        result["structuredContent"]
    )


@pytest.mark.parametrize("value", [None, "execute", [], True])
def test_invalid_mode_rejected_before_retrieval(value):
    query_tool = tool()
    with pytest.raises(ToolInputError):
        query_tool.call({"query": "RF", "response_format": value})
    query_tool.hybrid_search.search_with_details.assert_not_called()
