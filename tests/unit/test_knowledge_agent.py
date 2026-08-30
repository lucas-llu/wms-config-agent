from __future__ import annotations

from agents import Evidence, EvidenceStatus
from agents.nodes import KnowledgeAgent
from agents.tools import KnowledgeSearchResult


class FakeAdapter:
    def __init__(self, *, unsupported: set[str] | None = None, failing: set[str] | None = None):
        self.unsupported = unsupported or set()
        self.failing = failing or set()
        self.calls = []

    def search(self, query, *, filters, top_k=5, trace=None):
        del trace
        self.calls.append((query, filters, top_k))
        requirement = query.split("Required evidence: ", 1)[1].split(".", 1)[0]
        if requirement in self.failing:
            raise RuntimeError("retrieval unavailable")
        if requirement in self.unsupported:
            return KnowledgeSearchResult(query, filters, (), False, ())
        evidence = Evidence(
            evidence_id="evidence:shared",
            chunk_id="chunk:shared",
            source="manuals/shared.pdf",
            excerpt="Shared evidence excerpt",
            score=0.9,
            product_version="2024.1",
            module="inbound",
        )
        return KnowledgeSearchResult(query, filters, (evidence,), True, ())


def _task(task_id: str, *requirements: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "title": "Appointment capacity",
        "module": "inbound",
        "goal": "Define appointment capacity behavior",
        "evidence_requirements": list(requirements),
    }


def test_agent_builds_standalone_scoped_queries_and_stable_deduplicated_registry() -> None:
    adapter = FakeAdapter()
    agent = KnowledgeAgent(adapter, max_retrieval_tasks=4, max_parallel=2)

    result = agent.collect(
        tasks=[
            _task("task:two", "receiving documentation"),
            _task("task:one", "那个预约配置的容量证据"),
        ],
        confirmed_context={
            "product_version": "2024.1",
            "site": "DC01",
            "environment": "test",
        },
        existing_evidence=[],
    )

    assert [binding.task_id for binding in result.bindings] == ["task:one", "task:two"]
    assert all(binding.evidence_status is EvidenceStatus.SUPPORTED for binding in result.bindings)
    assert len(result.evidence) == 1
    assert result.knowledge_fingerprint.startswith("knowledge:")
    query = result.bindings[0].queries[0]
    assert "Appointment capacity" in query.query
    assert "Define appointment capacity behavior" in query.query
    assert query.filters == {
        "environment": "test",
        "module": "inbound",
        "site": "DC01",
        "version": "2024.1",
    }


def test_agent_marks_partial_and_unsupported_without_promoting_missing_evidence() -> None:
    adapter = FakeAdapter(
        unsupported={"missing documentation"},
        failing={"broken retrieval"},
    )
    agent = KnowledgeAgent(adapter, max_retrieval_tasks=4)

    result = agent.collect(
        tasks=[
            _task("task:partial", "supported documentation", "missing documentation"),
            _task("task:unsupported", "broken retrieval"),
        ],
        confirmed_context={"product_version": "2024.1"},
        existing_evidence=[],
    )

    bindings = {binding.task_id: binding for binding in result.bindings}
    assert bindings["task:partial"].evidence_status is EvidenceStatus.PARTIAL
    assert bindings["task:partial"].evidence_ids == ("evidence:shared",)
    assert bindings["task:partial"].gap_reasons == ("insufficient_evidence:missing documentation",)
    assert bindings["task:unsupported"].evidence_status is EvidenceStatus.UNSUPPORTED
    assert bindings["task:unsupported"].evidence_ids == ()
    assert bindings["task:unsupported"].gap_reasons == ("retrieval_error:RuntimeError",)


def test_retrieval_budget_creates_explicit_gap_and_counts_only_real_calls() -> None:
    adapter = FakeAdapter()
    agent = KnowledgeAgent(adapter, max_retrieval_tasks=1)

    result = agent.collect(
        tasks=[_task("task:budget", "first evidence", "second evidence")],
        confirmed_context={},
        existing_evidence=[],
    )

    binding = result.bindings[0]
    assert result.tool_calls_made == 1
    assert len(adapter.calls) == 1
    assert binding.evidence_status is EvidenceStatus.PARTIAL
    assert binding.gap_reasons == ("retrieval_budget_exceeded:second evidence",)
