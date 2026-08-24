"""MCP document metadata and summary tool."""

from __future__ import annotations

import json
from typing import Any

from mcp_server.catalog import CorpusCatalog
from mcp_server.tool_registry import MCPTool, ToolInputError


class GetDocumentSummaryTool:
    def __init__(self, catalog: CorpusCatalog) -> None:
        self.catalog = catalog

    def definition(self) -> MCPTool:
        return MCPTool(
            name="get_wms_document_summary",
            title="Get WMS Document Summary",
            description=(
                "Get metadata and an extractive summary by document ID, source path, "
                "or SWL process code. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {"document_id": {"type": "string", "minLength": 1}},
                "required": ["document_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"documents": {"type": "array", "items": {"type": "object"}}},
                "required": ["documents"],
            },
            handler=self.call,
        )

    def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"document_id"}:
            raise ToolInputError("document_id is the only supported argument")
        identifier = arguments.get("document_id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ToolInputError("document_id must be a non-empty string")
        documents = self.catalog.find_documents(identifier)
        if not documents:
            raise ToolInputError(f"Document not found: {identifier}")
        structured = {"documents": [document.to_dict() for document in documents]}
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(structured, ensure_ascii=False, indent=2),
                }
            ],
            "structuredContent": structured,
            "isError": False,
        }
