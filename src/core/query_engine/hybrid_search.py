"""Hybrid dense/sparse search orchestration with graceful degradation."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any

from core.query_engine.dense_retriever import DenseRetriever
from core.query_engine.fusion import ReciprocalRankFusion
from core.query_engine.query_processor import (
    ProcessedQuery,
    QueryProcessor,
    build_store_filters,
)
from core.query_engine.sparse_retriever import SparseRetriever
from core.settings import RetrievalSettings, Settings
from core.types import RetrievalResult

_SEARCH_TOKEN = re.compile(r"[A-Za-z0-9_.$-]{2,}|[\u4e00-\u9fff]")


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    processed_query: ProcessedQuery
    dense_results: tuple[RetrievalResult, ...]
    sparse_results: tuple[RetrievalResult, ...]
    fused_results: tuple[RetrievalResult, ...]
    results: tuple[RetrievalResult, ...]
    failures: dict[str, str]
    evidence_sufficient: bool


class HybridSearch:
    def __init__(
        self,
        settings: Settings | RetrievalSettings,
        query_processor: QueryProcessor,
        dense_retriever: DenseRetriever,
        sparse_retriever: SparseRetriever,
        fusion: ReciprocalRankFusion,
    ) -> None:
        self.settings = settings.retrieval if isinstance(settings, Settings) else settings
        self.query_processor = query_processor
        self.dense_retriever = dense_retriever
        self.sparse_retriever = sparse_retriever
        self.fusion = fusion

    def search(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
        trace: Any | None = None,
    ) -> list[RetrievalResult]:
        return list(self.search_with_details(query, top_k, filters, trace=trace).results)

    def search_with_details(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
        trace: Any | None = None,
    ) -> SearchOutcome:
        final_count = self.settings.top_k_final if top_k is None else top_k
        if final_count <= 0:
            raise ValueError("top_k must be greater than 0")
        started = time.perf_counter()
        processed = self.query_processor.process(query, filters)
        self._record_stage(
            trace,
            "query_processing",
            started,
            {
                "method": type(self.query_processor).__name__,
                "provider": type(self.query_processor).__module__,
                "keyword_count": len(processed.keywords),
                "filters": processed.filters,
            },
        )
        store_filters = build_store_filters(processed.filters)

        failures: dict[str, str] = {}
        dense: list[RetrievalResult] = []
        sparse: list[RetrievalResult] = []
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="retrieval") as pool:
            futures = {
                "dense": pool.submit(
                    self._retrieve_with_trace,
                    "dense_retrieval",
                    self.dense_retriever.retrieve,
                    trace,
                    processed.retrieval_query,
                    self.settings.top_k_dense,
                    store_filters,
                ),
                "sparse": pool.submit(
                    self._retrieve_with_trace,
                    "sparse_retrieval",
                    self.sparse_retriever.retrieve,
                    trace,
                    processed.keywords,
                    self.settings.top_k_sparse,
                    processed.filters,
                ),
            }
            for source, future in futures.items():
                try:
                    if source == "dense":
                        dense = future.result()
                    else:
                        sparse = future.result()
                except Exception as exc:
                    failures[source] = f"{type(exc).__name__}: {exc}"

        started = time.perf_counter()
        fused = self.fusion.fuse({"dense": dense, "sparse": sparse})
        boosted = self._boost_metadata_matches(fused, processed)
        filtered = self._apply_metadata_filters(boosted, processed.filters)
        diversified = self._diversify(filtered, final_count)
        self._record_stage(
            trace,
            "fusion",
            started,
            {
                "method": type(self.fusion).__name__,
                "provider": type(self.fusion).__module__,
                "dense_count": len(dense),
                "sparse_count": len(sparse),
                "result_count": len(diversified),
                "rankings": {
                    "dense": self._rank_snapshot(dense),
                    "sparse": self._rank_snapshot(sparse),
                    "fused": self._rank_snapshot(fused),
                    "final": self._rank_snapshot(diversified),
                },
            },
        )
        evidence_sufficient = (
            bool(diversified)
            and self._has_query_evidence(processed, diversified)
            and (diversified[0].score >= self.settings.min_fused_score or bool(failures))
        )
        return SearchOutcome(
            processed_query=processed,
            dense_results=tuple(dense),
            sparse_results=tuple(sparse),
            fused_results=tuple(fused),
            results=tuple(diversified),
            failures=failures,
            evidence_sufficient=evidence_sufficient,
        )

    @staticmethod
    def _retrieve_with_trace(
        stage_name: str,
        retrieve: Any,
        trace: Any | None,
        query: Any,
        top_k: int,
        filters: dict[str, Any] | None,
    ) -> list[RetrievalResult]:
        started = time.perf_counter()
        owner = getattr(retrieve, "__self__", None)
        method = type(owner).__name__ if owner is not None else stage_name
        provider_component = (
            getattr(owner, "embedding", None)
            if stage_name == "dense_retrieval"
            else getattr(owner, "bm25_indexer", None)
        )
        provider = (
            type(provider_component).__name__
            if provider_component is not None
            else type(owner).__module__
            if owner is not None
            else "unknown"
        )
        try:
            results = retrieve(query, top_k, filters, trace)
        except Exception as exc:
            HybridSearch._record_stage(
                trace,
                stage_name,
                started,
                {
                    "method": method,
                    "provider": provider,
                    "status": "error",
                    "error_type": type(exc).__name__,
                },
            )
            raise
        HybridSearch._record_stage(
            trace,
            stage_name,
            started,
            {
                "method": method,
                "provider": provider,
                "status": "ok",
                "result_count": len(results),
                "results": HybridSearch._rank_snapshot(results),
            },
        )
        return results

    @staticmethod
    def _rank_snapshot(results: list[RetrievalResult]) -> list[dict[str, Any]]:
        """Return a privacy-safe ranking snapshot without chunk text or metadata."""
        return [
            {
                "chunk_id": result.chunk_id,
                "rank": rank,
                "score": round(float(result.score), 8),
                "source_scores": {
                    key: round(float(value), 8)
                    for key, value in sorted(result.source_scores.items())
                },
                "source_ranks": dict(sorted(result.source_ranks.items())),
            }
            for rank, result in enumerate(results, start=1)
        ]

    @staticmethod
    def _boost_metadata_matches(
        candidates: list[RetrievalResult], query: ProcessedQuery
    ) -> list[RetrievalResult]:
        """Use trusted title metadata to disambiguate closely related WMS documents."""
        query_terms = {
            HybridSearch._normalize_metadata_term(term)
            for term in query.specific_terms
            if len(term) >= 2 and term.isascii()
        }
        query_terms.discard("")
        if not query_terms:
            return candidates
        boosted: list[RetrievalResult] = []
        for candidate in candidates:
            title = str(candidate.metadata.get("title", ""))
            title_terms = {
                HybridSearch._normalize_metadata_term(term.lower())
                for term in _SEARCH_TOKEN.findall(title)
                if len(term) >= 2 and term.isascii()
            }
            match_count = len(query_terms.intersection(title_terms))
            bonus = min(match_count * 0.006, 0.024)
            if title_terms and title_terms.issubset(query_terms):
                bonus += 0.008
            if not bonus:
                boosted.append(candidate)
                continue
            source_scores = dict(candidate.source_scores)
            source_scores["metadata_boost"] = bonus
            boosted.append(
                replace(
                    candidate,
                    score=candidate.score + bonus,
                    source_scores=source_scores,
                )
            )
        return sorted(boosted, key=lambda result: (-result.score, result.chunk_id))

    @staticmethod
    def _normalize_metadata_term(term: str) -> str:
        normalized = term.lower()
        if len(normalized) > 4 and normalized.endswith("s"):
            return normalized[:-1]
        return normalized

    @staticmethod
    def _record_stage(
        trace: Any | None,
        name: str,
        started: float,
        details: dict[str, Any],
    ) -> None:
        if trace is not None and hasattr(trace, "record_stage"):
            trace.record_stage(name, (time.perf_counter() - started) * 1000, details=details)

    @staticmethod
    def _apply_metadata_filters(
        candidates: list[RetrievalResult], filters: dict[str, Any]
    ) -> list[RetrievalResult]:
        if not filters:
            return candidates
        return [
            candidate
            for candidate in candidates
            if all(candidate.metadata.get(key) == value for key, value in filters.items())
        ]

    def _diversify(self, candidates: list[RetrievalResult], top_k: int) -> list[RetrievalResult]:
        counts: dict[str, int] = {}
        results: list[RetrievalResult] = []
        for candidate in candidates:
            document_key = str(
                candidate.metadata.get("file_hash")
                or candidate.metadata.get("source_path")
                or candidate.chunk_id
            )
            count = counts.get(document_key, 0)
            if count >= self.settings.max_chunks_per_document:
                continue
            counts[document_key] = count + 1
            results.append(candidate)
            if len(results) == top_k:
                break
        return results

    @staticmethod
    def _has_query_evidence(query: ProcessedQuery, candidates: list[RetrievalResult]) -> bool:
        if not query.specific_terms:
            return False
        candidate_terms: set[str] = set()
        for candidate in candidates:
            searchable = " ".join(
                [
                    candidate.text,
                    str(candidate.metadata.get("title", "")),
                    str(candidate.metadata.get("process_code", "")),
                    str(candidate.metadata.get("process_stage", "")),
                    str(candidate.metadata.get("domain", "")),
                ]
            )
            candidate_terms.update(token.lower() for token in _SEARCH_TOKEN.findall(searchable))
        matched_terms = candidate_terms.intersection(query.specific_terms)
        required_matches = min(2, len(query.specific_terms))
        return len(matched_terms) >= required_matches
