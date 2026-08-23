"""Build an extractive citation-first response without an enabled LLM."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.query_engine import SearchOutcome
from core.response.citation_generator import Citation, CitationGenerator


@dataclass(frozen=True, slots=True)
class EvidenceResponse:
    status: str
    query: str
    message: str
    markdown: str
    citations: tuple[Citation, ...]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResponseBuilder:
    def __init__(self, citation_generator: CitationGenerator | None = None) -> None:
        self.citation_generator = citation_generator or CitationGenerator()

    def build(self, outcome: SearchOutcome) -> EvidenceResponse:
        diagnostics = {
            "dense_count": len(outcome.dense_results),
            "sparse_count": len(outcome.sparse_results),
            "fused_count": len(outcome.fused_results),
            "returned_count": len(outcome.results),
            "filters": outcome.processed_query.filters,
            "expansions": outcome.processed_query.expansions,
            "failures": outcome.failures,
        }
        if not outcome.evidence_sufficient:
            message = "未找到足够可靠的文档证据，请补充流程编码、模块或配置名称。"
            return EvidenceResponse(
                status="insufficient_evidence",
                query=outcome.processed_query.original_query,
                message=message,
                markdown=message,
                citations=(),
                diagnostics=diagnostics,
            )

        citations = self.citation_generator.generate(list(outcome.results))
        lines = [
            f"检索到 {len(citations)} 条可核验的 WMS 文档证据。",
            "",
        ]
        for citation in citations:
            code = f" ({citation.process_code})" if citation.process_code else ""
            lines.extend(
                [
                    f"[{citation.index}] {citation.title}{code} — {citation.page_label}",
                    citation.excerpt,
                    f"来源：{citation.source}",
                    "",
                ]
            )
        message = "已找到相关文档证据；当前 LLM 未启用，因此返回原文片段而不推测配置结论。"
        return EvidenceResponse(
            status="evidence_found",
            query=outcome.processed_query.original_query,
            message=message,
            markdown="\n".join(lines).rstrip(),
            citations=tuple(citations),
            diagnostics=diagnostics,
        )
