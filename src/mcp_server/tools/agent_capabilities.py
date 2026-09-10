"""Strict, privacy-safe capability discovery for the opt-in Agent runtime."""

from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from core.settings import Settings
from mcp_server.protocol_handler import SUPPORTED_PROTOCOL_VERSIONS
from mcp_server.tool_registry import MCPTool, ToolInputError


class AgentCapabilitiesTool:
    def __init__(self, settings: Settings, registered_tools: list[MCPTool]) -> None:
        self.settings = settings
        self.registered_tools = tuple(registered_tools)
        self.template = self._load_template()

    def definition(self) -> MCPTool:
        return MCPTool(
            name="get_agent_capabilities",
            title="Get Agent Capabilities",
            description=(
                "Return versioned Agent, knowledge, tool, budget and safety capabilities. "
                "Never returns credentials or private content."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema=_output_schema(),
            annotations={
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            handler=self.call,
        )

    def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ToolInputError("get_agent_capabilities accepts no arguments")
        payload = self.payload()
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"WMS Configuration Agent {payload['product']['version']} is "
                        f"{'enabled' if payload['features']['agent_enabled'] else 'disabled'}; "
                        f"{len(payload['tools'])} tools are discoverable."
                    ),
                }
            ],
            "structuredContent": payload,
            "isError": False,
        }

    def payload(self) -> dict[str, Any]:
        modules = sorted(
            {
                str(item.get("module", "")).strip()
                for item in self.template.get("task_hints", [])
                if str(item.get("module", "")).strip()
            }
        )
        tools = [
            {
                "name": item.name,
                "title": item.title,
                "annotations": dict(item.annotations),
            }
            for item in sorted(self.registered_tools, key=lambda tool: tool.name)
        ]
        tools.append(
            {
                "name": "get_agent_capabilities",
                "title": "Get Agent Capabilities",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            }
        )
        tools.sort(key=lambda item: item["name"])
        api_key_name = self.settings.llm.api_key_env
        return {
            "schema_version": 1,
            "product": {
                "name": self.settings.project.name,
                "version": _package_version(),
                "agent_contract_version": "agent-state-v1",
            },
            "transport": {
                "kind": "stdio",
                "authentication": "host_process",
                "network_exposed": False,
                "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            },
            "features": {
                "agent_enabled": self.settings.agent.enabled,
                "approval_required": self.settings.agent.approval_required,
                "environment_inspector_enabled": (
                    self.settings.agent.environment_inspector_enabled
                ),
                "real_wms_write_enabled": False,
                "multi_tenant": False,
            },
            "provider": {
                "name": self.settings.llm.provider,
                "model": self.settings.llm.model,
                "credentials_available": bool(api_key_name and os.environ.get(api_key_name)),
            },
            "knowledge": {
                "ingestion_file_types": ["PDF"],
                "ingestion_extensions": [".pdf"],
                "planning_template": str(self.template.get("template_id", "unknown")),
                "modules": modules,
            },
            "budgets": {
                "max_nodes_per_turn": self.settings.agent.max_nodes_per_turn,
                "max_retrieval_tasks": self.settings.agent.max_retrieval_tasks,
                "max_self_repair_rounds": self.settings.agent.max_self_repair_rounds,
                "max_tokens_per_turn": self.settings.agent.max_tokens_per_turn,
                "turn_timeout_seconds": self.settings.agent.turn_timeout_seconds,
            },
            "exports": {"formats": ["json", "markdown"], "requires_approval": True},
            "tools": tools,
            "safety": {
                "citation_required": True,
                "blocking_conflicts_stop_review": True,
                "absolute_paths_redacted": True,
                "private_content_in_capabilities": False,
            },
        }

    def _load_template(self) -> dict[str, Any]:
        payload = json.loads(self.settings.agent.planning_template_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("planning template must be a JSON object")
        return payload


def _package_version() -> str:
    try:
        return version("wms-config-agent")
    except PackageNotFoundError:
        return "0.1.0"


def _output_schema() -> dict[str, Any]:
    def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    text = {"type": "string"}
    boolean = {"type": "boolean"}
    integer = {"type": "integer"}
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "product": object_schema(
                {"name": text, "version": text, "agent_contract_version": text},
                ["name", "version", "agent_contract_version"],
            ),
            "transport": object_schema(
                {
                    "kind": {"type": "string", "const": "stdio"},
                    "authentication": {"type": "string", "const": "host_process"},
                    "network_exposed": boolean,
                    "protocol_versions": {"type": "array", "items": text},
                },
                ["kind", "authentication", "network_exposed", "protocol_versions"],
            ),
            "features": object_schema(
                {
                    "agent_enabled": boolean,
                    "approval_required": boolean,
                    "environment_inspector_enabled": boolean,
                    "real_wms_write_enabled": boolean,
                    "multi_tenant": boolean,
                },
                [
                    "agent_enabled",
                    "approval_required",
                    "environment_inspector_enabled",
                    "real_wms_write_enabled",
                    "multi_tenant",
                ],
            ),
            "provider": object_schema(
                {
                    "name": text,
                    "model": {"type": ["string", "null"]},
                    "credentials_available": boolean,
                },
                ["name", "model", "credentials_available"],
            ),
            "knowledge": object_schema(
                {
                    "ingestion_file_types": {"type": "array", "items": text},
                    "ingestion_extensions": {"type": "array", "items": text},
                    "planning_template": text,
                    "modules": {"type": "array", "items": text},
                },
                [
                    "ingestion_file_types",
                    "ingestion_extensions",
                    "planning_template",
                    "modules",
                ],
            ),
            "budgets": object_schema(
                {
                    "max_nodes_per_turn": integer,
                    "max_retrieval_tasks": integer,
                    "max_self_repair_rounds": integer,
                    "max_tokens_per_turn": integer,
                    "turn_timeout_seconds": {"type": "number"},
                },
                [
                    "max_nodes_per_turn",
                    "max_retrieval_tasks",
                    "max_self_repair_rounds",
                    "max_tokens_per_turn",
                    "turn_timeout_seconds",
                ],
            ),
            "exports": object_schema(
                {
                    "formats": {"type": "array", "items": text},
                    "requires_approval": boolean,
                },
                ["formats", "requires_approval"],
            ),
            "tools": {
                "type": "array",
                "items": object_schema(
                    {
                        "name": text,
                        "title": text,
                        "annotations": object_schema(
                            {
                                "readOnlyHint": boolean,
                                "destructiveHint": boolean,
                                "idempotentHint": boolean,
                                "openWorldHint": boolean,
                            },
                            [
                                "readOnlyHint",
                                "destructiveHint",
                                "idempotentHint",
                                "openWorldHint",
                            ],
                        ),
                    },
                    ["name", "title", "annotations"],
                ),
            },
            "safety": object_schema(
                {
                    "citation_required": boolean,
                    "blocking_conflicts_stop_review": boolean,
                    "absolute_paths_redacted": boolean,
                    "private_content_in_capabilities": boolean,
                },
                [
                    "citation_required",
                    "blocking_conflicts_stop_review",
                    "absolute_paths_redacted",
                    "private_content_in_capabilities",
                ],
            ),
        },
        "required": [
            "schema_version",
            "product",
            "transport",
            "features",
            "provider",
            "knowledge",
            "budgets",
            "exports",
            "tools",
            "safety",
        ],
        "additionalProperties": False,
    }
