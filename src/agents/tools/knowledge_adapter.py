"""Read-only adapter from Agent evidence requests to the existing V1 RAG stack."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from agents.contracts import Evidence, stable_contract_id
from core.query_engine import HybridSearch, SafeReranker
from core.response import Citation, ResponseBuilder


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    query: str
    filters: dict[str, str]
    evidence: tuple[Evidence, ...]
    evidence_sufficient: bool
    failures: tuple[str, ...]


class KnowledgeAdapter:
    """Reuse hybrid retrieval, safe reranking, and citation-first response building."""

    def __init__(
        self,
        hybrid_search: HybridSearch,
        reranker: SafeReranker,
        response_builder: ResponseBuilder,
    ) -> None:
        self.hybrid_search = hybrid_search
        self.reranker = reranker
        self.response_builder = response_builder

    def search(
        self,
        query: str,
        *,
        filters: dict[str, str],
        top_k: int = 5,
        trace: Any | None = None,
    ) -> KnowledgeSearchResult:
        outcome = self.hybrid_search.search_with_details(
            query,
            top_k,
            filters,
            trace=trace,
        )
        reranked = self.reranker.rerank(
            outcome.processed_query.retrieval_query,
            list(outcome.results),
            trace=trace,
        )
        outcome = replace(outcome, results=reranked.results)
        response = self.response_builder.build(outcome)
        evidence = (
            _evidence_from_citations(response.citations)
            if response.status == "evidence_found"
            else ()
        )
        failures = tuple(
            [
                *(f"{source}:{message}" for source, message in sorted(outcome.failures.items())),
                *([f"rerank:{reranked.failure}"] if reranked.failure else []),
            ]
        )
        return KnowledgeSearchResult(
            query=query,
            filters=dict(sorted(filters.items())),
            evidence=evidence,
            evidence_sufficient=bool(evidence) and outcome.evidence_sufficient,
            failures=failures,
        )


def build_scope_filters(
    confirmed_context: dict[str, Any],
    *,
    module: str,
) -> dict[str, str]:
    """Map confirmed Agent scope to the V1 metadata vocabulary."""

    filters: dict[str, str] = {}
    product_version = _optional_text(confirmed_context.get("product_version"))
    if product_version:
        filters["version"] = product_version
    if normalized_module := _optional_text(module):
        filters["module"] = normalized_module
    for field in ("site", "environment"):
        if value := _optional_text(confirmed_context.get(field)):
            filters[field] = value
    return dict(sorted(filters.items()))


def evidence_registry_fingerprint(evidence: tuple[Evidence, ...]) -> str:
    return stable_contract_id(
        "knowledge",
        [item.to_dict() for item in sorted(evidence, key=lambda item: item.evidence_id)],
    )


def _evidence_from_citations(citations: tuple[Citation, ...]) -> tuple[Evidence, ...]:
    registry: dict[str, Evidence] = {}
    for citation in citations:
        source = _safe_source(citation.source)
        identifier = stable_contract_id(
            "evidence",
            {
                "chunk_id": citation.chunk_id,
                "source": source,
                "page_start": citation.page_start,
                "page_end": citation.page_end,
            },
        )
        registry[identifier] = Evidence(
            evidence_id=identifier,
            chunk_id=citation.chunk_id,
            source=source,
            excerpt=citation.excerpt,
            score=citation.score,
            page_start=citation.page_start,
            page_end=citation.page_end,
            product_version=_optional_text(
                citation.metadata.get("version") or citation.metadata.get("product_version")
            ),
            module=_optional_text(citation.metadata.get("module")),
            site=_optional_text(citation.metadata.get("site")),
            environment=_optional_text(citation.metadata.get("environment")),
            collection=_optional_text(citation.metadata.get("collection")),
        )
    return tuple(registry[key] for key in sorted(registry))


def _safe_source(value: str) -> str:
    windows_source = PureWindowsPath(value)
    posix_source = PurePosixPath(value.replace("\\", "/"))
    if windows_source.drive or windows_source.root:
        return windows_source.name
    if posix_source.is_absolute():
        return posix_source.name
    return posix_source.as_posix()


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
