"""Read-only action discovery from the live MCP registry."""

from __future__ import annotations

from typing import Any

from mcp_server.tool_registry import MCPTool, ToolInputError, ToolRegistry

_REQUIREMENTS = {
    "continue_configuration_session": ["current_revision", "resumable_session"],
    "get_configuration_session": ["workspace_session"],
    "validate_configuration_draft": ["current_revision", "workspace_session"],
    "review_configuration_draft": ["current_revision", "review_required", "explicit_decision"],
    "export_configuration_solution": ["current_revision", "approved_revision"],
}


class ActionCatalogTool:
    def __init__(self, registry: ToolRegistry, workspace_id: str) -> None:
        self.registry = registry
        self.workspace_id = workspace_id

    def definition(self) -> MCPTool:
        return MCPTool(
            name="get_agent_actions",
            title="Get Agent Actions",
            description="List registered actions, annotations and execution prerequisites. "
            "Registration is not session authorization or a provider health check.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=_schema(),
            handler=self.call,
        )

    def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ToolInputError("get_agent_actions accepts no arguments")
        actions = []
        for definition in self.registry.definitions():
            name = definition["name"]
            annotations = definition["annotations"]
            actions.append(
                {
                    "name": name,
                    "title": definition["title"],
                    "annotations": annotations,
                    "availability": "registered",
                    "permission_boundary": "host_process_and_workspace",
                    "effect": "read_only"
                    if annotations.get("readOnlyHint")
                    else "local_state_change",
                    "prerequisites": _REQUIREMENTS.get(name, []),
                    "requires_approved_revision": name == "export_configuration_solution",
                }
            )
        payload = {"schema_version": 1, "workspace_id": self.workspace_id, "actions": actions}
        lines = ["Available actions (subject to workspace and session checks):", ""]
        for action in actions:
            prerequisites = ", ".join(action["prerequisites"]) or "tool input validation"
            lines.append(f"- {action['name']}: {action['effect']}; {prerequisites}")
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structuredContent": payload,
            "isError": False,
        }


def _schema() -> dict[str, Any]:
    annotations = {
        key: {"type": "boolean"}
        for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    }
    fields = {
        "name": {"type": "string"},
        "title": {"type": "string"},
        "annotations": {
            "type": "object",
            "properties": annotations,
            "required": list(annotations),
            "additionalProperties": False,
        },
        "availability": {"const": "registered", "type": "string"},
        "permission_boundary": {"const": "host_process_and_workspace", "type": "string"},
        "effect": {"type": "string", "enum": ["read_only", "local_state_change"]},
        "prerequisites": {"type": "array", "items": {"type": "string"}},
        "requires_approved_revision": {"type": "boolean"},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "workspace_id", "actions"],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "workspace_id": {"type": "string"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": fields,
                    "required": list(fields),
                    "additionalProperties": False,
                },
            },
        },
    }
