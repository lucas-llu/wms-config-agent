"""MCP collection catalog tool."""

from __future__ import annotations

import json
from typing import Any

from mcp_server.catalog import CorpusCatalog
from mcp_server.tool_registry import MCPTool, ToolInputError


class ListCollectionsTool:
    def __init__(self, catalog: CorpusCatalog) -> None:
        self.catalog = catalog

    def definition(self) -> MCPTool:
        return MCPTool(
            name="list_wms_collections",
            title="List WMS Knowledge Collections",
            description="List local authorized WMS document collections and counts. Read-only.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={
                "type": "object",
                "properties": {"collections": {"type": "array", "items": {"type": "object"}}},
                "required": ["collections"],
            },
            handler=self.call,
        )

    def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ToolInputError("list_wms_collections does not accept arguments")
        structured = {"collections": self.catalog.list_collections()}
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
