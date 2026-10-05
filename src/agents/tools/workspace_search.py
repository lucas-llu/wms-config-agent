"""Workspace boundary shared by V1 queries and Agent knowledge retrieval."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agents.workspace import Workspace


class WorkspaceSearch:
    def __init__(self, search: Any, workspace: Workspace) -> None:
        self.search_backend = search
        self.workspace = workspace

    def search_with_details(self, query, top_k=None, filters=None, trace=None):
        scoped = self.workspace.filters(filters or {})
        outcome = self.search_backend.search_with_details(query, top_k, scoped, trace=trace)

        def visible(items):
            return tuple(item for item in items if self.workspace.permits_metadata(item.metadata))

        results = visible(outcome.results)
        return replace(
            outcome,
            dense_results=visible(outcome.dense_results),
            sparse_results=visible(outcome.sparse_results),
            fused_results=visible(outcome.fused_results),
            results=results,
            evidence_sufficient=outcome.evidence_sufficient and bool(results),
        )
