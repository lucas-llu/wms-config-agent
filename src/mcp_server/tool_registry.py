"""MCP tool definitions, registration, and invocation contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


class ToolInputError(ValueError):
    """An argument error that should be visible to the calling model."""


@dataclass(frozen=True, slots=True)
class MCPTool:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = field(
        default_factory=lambda: {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )

    def definition(self) -> dict[str, Any]:
        definition = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
        }
        if self.output_schema is not None:
            definition["outputSchema"] = self.output_schema
        return definition


class ToolRegistry:
    def __init__(self, tools: list[MCPTool] | None = None) -> None:
        self._tools: dict[str, MCPTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: MCPTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"MCP tool is already registered: {tool.name}")
        self._tools[tool.name] = tool

    def definitions(self) -> list[dict[str, Any]]:
        return [self._tools[name].definition() for name in sorted(self._tools)]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown MCP tool: {name}") from exc
        try:
            return tool.handler(arguments)
        except ToolInputError as exc:
            error = {"error": str(exc), "tool": name}
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "structuredContent": error,
                "isError": True,
            }
