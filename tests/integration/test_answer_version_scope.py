"""Version boundaries must survive retrieval, generation, citations and graph routing."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agents.contracts import Evidence
from agents.nodes.grounded_answer import answer_question
from agents.supervisor import Supervisor
from agents.tools import KnowledgeSearchResult
from agents.workspace import Workspace
from core.settings import load_settings
from libs.llm import ChatResponse


class NoModelCall:
    calls = 0

    def chat(self, messages):
        self.calls += 1
        pytest.fail("An incompatible version must be blocked before model generation")


def source(version, identifier="1"):
    return Evidence(
        f"e:{identifier}",
        f"c:{identifier}",
        f"synthetic-{version}.pdf",
        "FEATURE_A records scan events.",
        1.0,
        product_version=version,
        module="inbound",
        site="TEST-A",
        environment="test",
    )


@pytest.mark.parametrize(
    "workspace",
    [
        None,
        Workspace(
            "workspace:scope_test",
            "Scope test",
            ("synthetic",),
            ("inbound",),
            ("TEST-A",),
            ("test",),
        ),
    ],
)
def test_confirmed_version_is_forwarded_to_retrieval_and_wrong_results_cannot_leak(workspace):
    class Search:
        filters = None

        def search(self, query, *, filters, top_k):
            self.filters = filters
            # Deliberately simulate a backend that ignores the requested version filter.
            return KnowledgeSearchResult(query, filters, (source("v2"),), True, ())

    search = Search()
    model = NoModelCall()
    supervisor = Supervisor(
        llm=model,
        settings=load_settings().agent,
        knowledge_adapter=search,
        workspace=workspace,
    )
    update = supervisor.graph._answer_question(
        {
            "status": "created",
            "intent": "atomic_query",
            "latest_user_message": "What is A?",
            "confirmed_context": {
                "product_version": "v1",
                "modules": ["inbound"],
                "site": "TEST-A",
                "environment": "test",
            },
        }
    )
    assert search.filters["version"] == "v1"
    assert search.filters["module"] == "inbound"
    assert search.filters["site"] == "TEST-A"
    if workspace:
        assert search.filters["collection"] == "synthetic"
    assert model.calls == 0
    assert update["answer_status"] == "insufficient_evidence"
    assert update["answer_evidence"] == []


def test_valid_matching_source_is_retained_and_attributed_to_its_original_index():
    class Model:
        def chat(self, messages):
            prompt = messages[0]["content"]
            assert '"id": "2"' in prompt and '"id": "1"' not in prompt
            return ChatResponse(
                json.dumps(
                    {
                        "status": "answered",
                        "gap": "",
                        "claims": [
                            {
                                "text": "FEATURE_A records scan events.",
                                "source_id": "2",
                                "quote": "FEATURE_A records scan events.",
                            }
                        ],
                    }
                ),
                metadata={"usage": {"total_tokens": 20}},
            )

    class Search:
        def search(self, query, *, filters, top_k):
            return KnowledgeSearchResult(
                query, filters, (source("v2"), source("v1", "2")), True, ()
            )

    graph = Supervisor(
        llm=Model(),
        settings=load_settings().agent,
        knowledge_adapter=Search(),
    ).graph
    result = graph._answer_question(
        {
            "status": "created",
            "intent": "atomic_query",
            "latest_user_message": "What is A?",
            "confirmed_context": {"product_version": "v1"},
        }
    )
    assert result["answer_status"] == "answered"
    assert len(result["answer_evidence"]) == 1
    assert result["answer_evidence"][0]["product_version"] == "v1"
    assert result["answer_evidence"][0]["citation_index"] == 2


@pytest.mark.parametrize("wanted,actual", [("v1", "v10"), ("2024.1", "2024.10"), ("v1", "v2")])
def test_version_comparison_is_not_a_substring_match(wanted, actual):
    result = answer_question(
        NoModelCall(),
        "What is A?",
        (source(actual),),
        confirmed_context={"product_version": wanted},
    )
    assert result.status == "insufficient_evidence"


def test_missing_metadata_remains_unknown_in_generation_context():
    class Model:
        def chat(self, messages):
            assert '"product_version": null' in messages[0]["content"]
            return ChatResponse(
                json.dumps(
                    {
                        "status": "insufficient_evidence",
                        "claims": [],
                        "gap": "Document version is unknown. Provide matching documentation.",
                    }
                )
            )

    result = answer_question(
        Model(),
        "What is A in v1?",
        (replace(source("v1"), product_version=None),),
        confirmed_context={"product_version": "v1"},
    )
    assert result.status == "insufficient_evidence" and result.cited_source_ids == ()
