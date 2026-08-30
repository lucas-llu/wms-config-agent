"""Deterministic Knowledge Agent for task-scoped evidence retrieval."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Protocol

from agents.contracts import (
    Evidence,
    EvidenceQuery,
    EvidenceStatus,
    TaskEvidenceBinding,
)
from agents.tools import (
    KnowledgeSearchResult,
    build_scope_filters,
    evidence_registry_fingerprint,
)


class KnowledgeCollectionError(ValueError):
    """Raised when planning state cannot form a safe evidence request."""


class KnowledgeSearch(Protocol):
    def search(
        self,
        query: str,
        *,
        filters: dict[str, str],
        top_k: int = 5,
        trace: Any | None = None,
    ) -> KnowledgeSearchResult: ...


@dataclass(frozen=True, slots=True)
class KnowledgeCollection:
    evidence: tuple[Evidence, ...]
    bindings: tuple[TaskEvidenceBinding, ...]
    knowledge_fingerprint: str
    tool_calls_made: int


@dataclass(frozen=True, slots=True)
class _TaskScope:
    task_id: str
    title: str
    module: str
    goal: str
    requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Request:
    task_id: str
    requirement: str
    query: str
    filters: dict[str, str]


@dataclass(frozen=True, slots=True)
class _RequestResult:
    request: _Request
    search: KnowledgeSearchResult | None
    gap_reason: str | None


class KnowledgeAgent:
    """Build standalone requests and merge bounded retrieval results deterministically."""

    def __init__(
        self,
        adapter: KnowledgeSearch,
        *,
        max_retrieval_tasks: int,
        max_parallel: int = 4,
        top_k: int = 5,
    ) -> None:
        if max_retrieval_tasks <= 0:
            raise ValueError("max_retrieval_tasks must be greater than 0")
        if max_parallel <= 0:
            raise ValueError("max_parallel must be greater than 0")
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0")
        self.adapter = adapter
        self.max_retrieval_tasks = max_retrieval_tasks
        self.max_parallel = min(max_parallel, max_retrieval_tasks)
        self.top_k = top_k

    def collect(
        self,
        *,
        tasks: list[dict[str, Any]],
        confirmed_context: dict[str, Any],
        existing_evidence: list[dict[str, Any]],
    ) -> KnowledgeCollection:
        scopes = tuple(
            sorted((_task_scope(value) for value in tasks), key=lambda item: item.task_id)
        )
        if not scopes:
            raise KnowledgeCollectionError("knowledge retrieval requires at least one task")
        requests = tuple(
            _request(scope, requirement, confirmed_context)
            for scope in scopes
            for requirement in scope.requirements
        )
        allowed = requests[: self.max_retrieval_tasks]
        skipped = requests[self.max_retrieval_tasks :]

        results = self._execute(allowed)
        results.extend(
            _RequestResult(
                request=request,
                search=None,
                gap_reason=f"retrieval_budget_exceeded:{request.requirement}",
            )
            for request in skipped
        )
        by_task: dict[str, list[_RequestResult]] = {scope.task_id: [] for scope in scopes}
        for result in sorted(
            results,
            key=lambda item: (item.request.task_id, item.request.requirement),
        ):
            by_task[result.request.task_id].append(result)

        registry = _existing_registry(existing_evidence)
        bindings: list[TaskEvidenceBinding] = []
        for scope in scopes:
            task_results = by_task[scope.task_id]
            evidence_ids: set[str] = set()
            gaps: list[str] = []
            queries: list[EvidenceQuery] = []
            supported_requirements = 0
            for result in task_results:
                queries.append(
                    EvidenceQuery(
                        requirement=result.request.requirement,
                        query=result.request.query,
                        filters=result.request.filters,
                    )
                )
                if (
                    result.search is not None
                    and result.search.evidence_sufficient
                    and result.search.evidence
                ):
                    supported_requirements += 1
                    for evidence in result.search.evidence:
                        registry[evidence.evidence_id] = evidence
                        evidence_ids.add(evidence.evidence_id)
                else:
                    gaps.append(
                        result.gap_reason or f"insufficient_evidence:{result.request.requirement}"
                    )

            status = _evidence_status(supported_requirements, len(task_results))
            bindings.append(
                TaskEvidenceBinding(
                    task_id=scope.task_id,
                    queries=tuple(queries),
                    evidence_ids=tuple(sorted(evidence_ids)),
                    evidence_status=status,
                    gap_reasons=tuple(dict.fromkeys(gaps)),
                )
            )

        evidence = tuple(registry[key] for key in sorted(registry))
        return KnowledgeCollection(
            evidence=evidence,
            bindings=tuple(bindings),
            knowledge_fingerprint=evidence_registry_fingerprint(evidence),
            tool_calls_made=len(allowed),
        )

    def _execute(self, requests: tuple[_Request, ...]) -> list[_RequestResult]:
        if not requests:
            return []
        results: list[_RequestResult] = []
        with ThreadPoolExecutor(
            max_workers=min(self.max_parallel, len(requests)),
            thread_name_prefix="knowledge",
        ) as pool:
            futures = {
                pool.submit(
                    self.adapter.search,
                    request.query,
                    filters=request.filters,
                    top_k=self.top_k,
                ): request
                for request in requests
            }
            for future in as_completed(futures):
                request = futures[future]
                try:
                    search = future.result()
                except Exception as exc:
                    results.append(
                        _RequestResult(
                            request=request,
                            search=None,
                            gap_reason=f"retrieval_error:{type(exc).__name__}",
                        )
                    )
                    continue
                gap = None
                if not search.evidence_sufficient:
                    gap = f"insufficient_evidence:{request.requirement}"
                results.append(_RequestResult(request, search, gap))
        return results

    def collect_targeted(
        self,
        *,
        tasks: list[dict[str, Any]],
        confirmed_context: dict[str, Any],
        existing_evidence: list[dict[str, Any]],
        existing_bindings: list[dict[str, Any]],
        requirements: dict[str, list[str]],
    ) -> KnowledgeCollection:
        targeted_tasks = []
        for task in tasks:
            task_id = str(task.get("task_id", ""))
            selected = requirements.get(task_id)
            if not selected:
                continue
            targeted_tasks.append({**task, "evidence_requirements": list(selected)})
        refreshed = self.collect(
            tasks=targeted_tasks,
            confirmed_context=confirmed_context,
            existing_evidence=existing_evidence,
        )
        bindings = _merge_bindings(existing_bindings, refreshed.bindings)
        return KnowledgeCollection(
            evidence=refreshed.evidence,
            bindings=bindings,
            knowledge_fingerprint=refreshed.knowledge_fingerprint,
            tool_calls_made=refreshed.tool_calls_made,
        )


def _task_scope(value: dict[str, Any]) -> _TaskScope:
    try:
        task_id = _required_text(value["task_id"], "task_id")
        title = _required_text(value["title"], "title")
        module = _required_text(value["module"], "module")
        goal = _required_text(value["goal"], "goal")
        requirements_value = value["evidence_requirements"]
    except KeyError as exc:
        raise KnowledgeCollectionError(f"task is missing field: {exc.args[0]}") from exc
    if not isinstance(requirements_value, list) or not requirements_value:
        raise KnowledgeCollectionError(f"task {task_id!r} must contain evidence requirements")
    requirements = tuple(
        dict.fromkeys(
            _required_text(item, "evidence_requirements item") for item in requirements_value
        )
    )
    return _TaskScope(task_id, title, module, goal, requirements)


def _request(
    scope: _TaskScope,
    requirement: str,
    confirmed_context: dict[str, Any],
) -> _Request:
    filters = build_scope_filters(confirmed_context, module=scope.module)
    scope_text = ", ".join(f"{key}={value}" for key, value in filters.items())
    query = (
        f"Task: {scope.title}. Goal: {scope.goal}. "
        f"Required evidence: {requirement}. Confirmed scope: {scope_text or 'none'}."
    )
    return _Request(scope.task_id, requirement, query[:2000], filters)


def _existing_registry(values: list[dict[str, Any]]) -> dict[str, Evidence]:
    registry: dict[str, Evidence] = {}
    for index, value in enumerate(values):
        try:
            evidence = Evidence(**value)
        except (TypeError, ValueError) as exc:
            raise KnowledgeCollectionError(f"evidence_registry[{index}] is invalid") from exc
        registry[evidence.evidence_id] = evidence
    return registry


def _evidence_status(supported: int, total: int) -> EvidenceStatus:
    if supported == total:
        return EvidenceStatus.SUPPORTED
    if supported:
        return EvidenceStatus.PARTIAL
    return EvidenceStatus.UNSUPPORTED


def _merge_bindings(
    existing: list[dict[str, Any]],
    refreshed: tuple[TaskEvidenceBinding, ...],
) -> tuple[TaskEvidenceBinding, ...]:
    merged = {_binding_from_state(value).task_id: _binding_from_state(value) for value in existing}
    for new in refreshed:
        old = merged.get(new.task_id)
        if old is None:
            merged[new.task_id] = new
            continue
        refreshed_requirements = {item.requirement for item in new.queries}
        queries = {item.requirement: item for item in old.queries}
        queries.update({item.requirement: item for item in new.queries})
        gaps = [
            gap
            for gap in old.gap_reasons
            if not any(requirement in gap for requirement in refreshed_requirements)
        ]
        gaps.extend(new.gap_reasons)
        evidence_ids = tuple(sorted(set(old.evidence_ids).union(new.evidence_ids)))
        unique_gaps = tuple(dict.fromkeys(gaps))
        status = (
            EvidenceStatus.PARTIAL
            if evidence_ids and unique_gaps
            else EvidenceStatus.SUPPORTED
            if evidence_ids
            else EvidenceStatus.UNSUPPORTED
        )
        merged[new.task_id] = TaskEvidenceBinding(
            task_id=new.task_id,
            queries=tuple(queries[key] for key in sorted(queries)),
            evidence_ids=evidence_ids,
            evidence_status=status,
            gap_reasons=unique_gaps,
        )
    return tuple(merged[key] for key in sorted(merged))


def _binding_from_state(value: dict[str, Any]) -> TaskEvidenceBinding:
    return TaskEvidenceBinding(
        task_id=str(value["task_id"]),
        queries=tuple(EvidenceQuery(**item) for item in value.get("queries", [])),
        evidence_ids=tuple(str(item) for item in value.get("evidence_ids", [])),
        evidence_status=EvidenceStatus(str(value["evidence_status"])),
        gap_reasons=tuple(str(item) for item in value.get("gap_reasons", [])),
    )


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeCollectionError(f"{field} must be a non-empty string")
    return value.strip()
