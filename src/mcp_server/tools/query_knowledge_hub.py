"""MCP tool that exposes citation-first hybrid WMS retrieval."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.query_engine import HybridSearch, SafeReranker
from core.response import MultimodalAssembler, ResponseBuilder
from core.trace import TraceCollector
from mcp_server.tool_registry import MCPTool, ToolInputError


class QueryKnowledgeHubTool:
    def __init__(
        self,
        hybrid_search: HybridSearch,
        reranker: SafeReranker,
        response_builder: ResponseBuilder,
        multimodal_assembler: MultimodalAssembler,
        trace_collector: TraceCollector | None = None,
    ) -> None:
        self.hybrid_search = hybrid_search
        self.reranker = reranker
        self.response_builder = response_builder
        self.multimodal_assembler = multimodal_assembler
        self.trace_collector = trace_collector

    def definition(self) -> MCPTool:
        return MCPTool(
            name="query_wms_knowledge",
            title="Query WMS Configuration Knowledge",
            description=(
                "Search authorized local WMS/JDA MOCA documentation and return "
                "verifiable source excerpts with page citations. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                    "collection": {"type": "string"},
                    "domain": {"type": "string"},
                    "document_type": {
                        "type": "string",
                        "enum": ["configuration", "operation"],
                    },
                    "process_code": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "query": {"type": "string"},
                    "message": {"type": "string"},
                    "markdown": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "object"}},
                    "diagnostics": {"type": "object"},
                },
                "required": ["status", "query", "message", "markdown", "citations"],
            },
            handler=self.call,
        )

    def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolInputError("query must be a non-empty string")
        top_k = arguments.get("top_k", 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise ToolInputError("top_k must be an integer between 1 and 20")
        filters = self._filters(arguments)
        trace = (
            self.trace_collector.start("query", {"query": query, "filters": filters})
            if self.trace_collector
            else None
        )
        try:
            outcome = self.hybrid_search.search_with_details(query, top_k, filters, trace=trace)
            reranked = self.reranker.rerank(
                outcome.processed_query.retrieval_query,
                list(outcome.results),
                trace=trace,
            )
            outcome = replace(outcome, results=reranked.results)
            response = self.response_builder.build(outcome)
            structured = self._sanitize_structured(response.to_dict())
            if reranked.failure:
                structured["diagnostics"]["rerank_failure"] = reranked.failure
            if trace:
                structured["diagnostics"]["trace_id"] = trace.trace_id
            content = [{"type": "text", "text": response.markdown}]
            content.extend(self.multimodal_assembler.assemble(list(outcome.results)))
            if trace:
                trace.finish()
            return {
                "content": content,
                "structuredContent": structured,
                "isError": False,
            }
        except Exception as exc:
            if trace:
                trace.finish(status="error", error=type(exc).__name__)
            raise
        finally:
            if self.trace_collector:
                self.trace_collector.collect(trace)

    @staticmethod
    def _filters(arguments: dict[str, Any]) -> dict[str, str]:
        filters: dict[str, str] = {}
        for key in ("collection", "domain", "document_type", "process_code"):
            value = arguments.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ToolInputError(f"{key} must be a non-empty string")
            filters[key] = value.strip()
        unknown = set(arguments) - {
            "query",
            "top_k",
            "collection",
            "domain",
            "document_type",
            "process_code",
        }
        if unknown:
            raise ToolInputError(f"Unsupported arguments: {', '.join(sorted(unknown))}")
        return filters

    @staticmethod
    def _sanitize_structured(payload: dict[str, Any]) -> dict[str, Any]:
        """Remove host filesystem paths and bulky preprocessing details at the MCP boundary."""
        for citation in payload.get("citations", []):
            metadata = citation.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("source_path", None)
                metadata.pop("pages", None)
        return payload
