"""Allowlisted adapters exposed to narrow Agent nodes."""

from agents.tools.knowledge_adapter import (
    KnowledgeAdapter,
    KnowledgeSearchResult,
    build_scope_filters,
    evidence_registry_fingerprint,
)

__all__ = [
    "KnowledgeAdapter",
    "KnowledgeSearchResult",
    "build_scope_filters",
    "evidence_registry_fingerprint",
]
