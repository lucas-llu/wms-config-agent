"""Hybrid dense/sparse search orchestration with graceful degradation."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
        self.settings = (
            settings.retrieval if isinstance(settings, Settings) else settings
        )
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
        return list(
            self.search_with_details(query, top_k, filters, trace=trace).results
        )

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
        processed = self.query_processor.process(query, filters)
        store_filters = build_store_filters(processed.filters)

        failures: dict[str, str] = {}
        dense: list[RetrievalResult] = []
        sparse: list[RetrievalResult] = []
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="retrieval") as pool:
            futures = {
                "dense": pool.submit(
                    self.dense_retriever.retrieve,
                    processed.retrieval_query,
                    self.settings.top_k_dense,
                    store_filters,
                    trace,
                ),
                "sparse": pool.submit(
                    self.sparse_retriever.retrieve,
                    processed.keywords,
                    self.settings.top_k_sparse,
                    processed.filters,
                    trace,
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

        fused = self.fusion.fuse({"dense": dense, "sparse": sparse})
        filtered = self._apply_metadata_filters(fused, processed.filters)
        diversified = self._diversify(filtered, final_count)
        evidence_sufficient = (
            bool(diversified)
            and self._has_query_evidence(processed, diversified)
            and (
                diversified[0].score >= self.settings.min_fused_score
                or bool(failures)
            )
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

    def _diversify(
        self, candidates: list[RetrievalResult], top_k: int
    ) -> list[RetrievalResult]:
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
    def _has_query_evidence(
        query: ProcessedQuery, candidates: list[RetrievalResult]
    ) -> bool:
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
            candidate_terms.update(
                token.lower() for token in _SEARCH_TOKEN.findall(searchable)
            )
        matched_terms = candidate_terms.intersection(query.specific_terms)
        required_matches = min(2, len(query.specific_terms))
        return len(matched_terms) >= required_matches
