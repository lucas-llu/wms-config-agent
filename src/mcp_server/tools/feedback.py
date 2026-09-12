"""MCP feedback recording and read-only evaluation summaries."""

from __future__ import annotations

import json
from typing import Any

from agents.repositories.feedback_repository import (
    FEEDBACK_KINDS,
    REGENERATION_REASONS,
    FeedbackRepository,
)
from agents.repositories.session_repository import SessionRepositoryError
from mcp_server.tool_registry import MCPTool, ToolInputError


class FeedbackTools:
    def __init__(self, repository: FeedbackRepository) -> None:
        self.repository = repository

    def definitions(self) -> list[MCPTool]:
        shared = {
            "session_id": {"type": "string", "minLength": 1},
            "revision": {"type": "integer", "minimum": 1},
        }
        return [
            MCPTool(
                name="record_configuration_feedback",
                title="Record Configuration Feedback",
                description="Record a deduplicated revision signal; no text, approval or regeneration.",
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        **shared,
                        "kind": {"enum": list(FEEDBACK_KINDS)},
                        "reason": {"enum": ["", *REGENERATION_REASONS]},
                    },
                    "required": ["session_id", "revision", "kind"],
                },
                handler=lambda args: self._call(args, write=True),
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
            MCPTool(
                name="get_configuration_feedback_summary",
                title="Get Configuration Feedback Summary",
                description="Read revision feedback counts; not an accuracy score or user vote tally.",
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": shared,
                    "required": list(shared),
                },
                handler=lambda args: self._call(args, write=False),
            ),
        ]

    def _call(self, arguments: dict[str, Any], *, write: bool) -> dict[str, Any]:
        allowed = (
            {"session_id", "revision", "kind", "reason"} if write else {"session_id", "revision"}
        )
        if set(arguments) - allowed:
            raise ToolInputError(
                "Unsupported feedback fields; free text and trace overrides forbidden"
            )
        revision = arguments.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ToolInputError("revision must be a positive integer")
        try:
            payload = (
                self.repository.record(**arguments)
                if write
                else self.repository.summary(**arguments)
            )
        except (TypeError, ValueError, SessionRepositoryError) as exc:
            raise ToolInputError("Invalid feedback input or inaccessible session revision") from exc
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": payload,
            "isError": False,
        }
