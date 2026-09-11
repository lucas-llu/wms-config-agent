"""MCP tool exposing the privacy-safe Knowledge catalog read model."""

from __future__ import annotations

import json
from typing import Any

from mcp_server.catalog import CorpusCatalog
from mcp_server.tool_registry import MCPTool, ToolInputError


class GetKnowledgeCatalogTool:
    def __init__(self, catalog: CorpusCatalog) -> None:
        self.catalog = catalog

    def definition(self) -> MCPTool:
        return MCPTool(
            name="get_wms_knowledge_catalog",
            title="Get WMS Knowledge Catalog",
            description=(
                "Return a read-only catalog of authorized WMS knowledge documents with version, "
                "workspace scope, index health and freshness. Never returns document bodies or "
                "absolute host paths."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "collection": {"type": "string", "minLength": 1},
                    "module": {"type": "string", "minLength": 1},
                    "scope_status": {
                        "type": "string",
                        "enum": ["complete", "incomplete"],
                    },
                    "freshness_status": {
                        "type": "string",
                        "enum": ["fresh", "stale", "unknown"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 10_000},
                },
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
        collection = _optional_text(arguments.get("collection"), "collection")
        module = _optional_text(arguments.get("module"), "module")
        scope_status = _optional_choice(
            arguments.get("scope_status"),
            "scope_status",
            {"complete", "incomplete"},
        )
        freshness_status = _optional_choice(
            arguments.get("freshness_status"),
            "freshness_status",
            {"fresh", "stale", "unknown"},
        )
        limit = _bounded_integer(arguments.get("limit", 100), "limit", minimum=1, maximum=200)
        offset = _bounded_integer(
            arguments.get("offset", 0),
            "offset",
            minimum=0,
            maximum=10_000,
        )
        unknown = set(arguments) - {
            "collection",
            "module",
            "scope_status",
            "freshness_status",
            "limit",
            "offset",
        }
        if unknown:
            raise ToolInputError(f"Unsupported arguments: {', '.join(sorted(unknown))}")
        structured = self.catalog.knowledge_snapshot(
            collection=collection,
            module=module,
            scope_status=scope_status,
            freshness_status=freshness_status,
            limit=limit,
            offset=offset,
        )
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


def _output_schema() -> dict[str, Any]:
    def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    nullable_text = {"type": ["string", "null"]}
    nullable_integer = {"type": ["integer", "null"]}
    nullable_boolean = {"type": ["boolean", "null"]}
    text = {"type": "string"}
    integer = {"type": "integer"}
    return object_schema(
        {
            "catalog_version": {"type": "integer", "const": 1},
            "workspace": object_schema(
                {
                    "workspace_id": text,
                    "scope_enforced": {"type": "boolean"},
                    "policy_mode": text,
                },
                ["workspace_id", "scope_enforced", "policy_mode"],
            ),
            "summary": object_schema(
                {
                    "document_count": integer,
                    "collection_count": integer,
                    "scope_complete_count": integer,
                    "scope_incomplete_count": integer,
                    "fresh_count": integer,
                    "stale_count": integer,
                    "unknown_freshness_count": integer,
                },
                [
                    "document_count",
                    "collection_count",
                    "scope_complete_count",
                    "scope_incomplete_count",
                    "fresh_count",
                    "stale_count",
                    "unknown_freshness_count",
                ],
            ),
            "index_health": object_schema(
                {
                    "status": {
                        "type": "string",
                        "enum": ["healthy", "degraded", "unavailable", "unverified"],
                    },
                    "scope": {"type": "string", "enum": ["shared_index", "workspace"]},
                    "dense_count": nullable_integer,
                    "sparse_count": nullable_integer,
                    "processed_chunk_count": integer,
                    "aligned": nullable_boolean,
                    "checked_at": text,
                },
                [
                    "status",
                    "scope",
                    "dense_count",
                    "sparse_count",
                    "processed_chunk_count",
                    "aligned",
                    "checked_at",
                ],
            ),
            "documents": {
                "type": "array",
                "items": object_schema(
                    {
                        "document_id": text,
                        "title": text,
                        "source": text,
                        "collection": text,
                        "domain": nullable_text,
                        "process_code": nullable_text,
                        "process_stage": nullable_text,
                        "document_type": nullable_text,
                        "page_count": nullable_integer,
                        "chunk_count": integer,
                        "scope": object_schema(
                            {
                                "version": nullable_text,
                                "module": nullable_text,
                                "site": nullable_text,
                                "environment": nullable_text,
                                "status": {
                                    "type": "string",
                                    "enum": ["complete", "incomplete"],
                                },
                                "missing_fields": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "enum": ["version", "module", "site", "environment"],
                                    },
                                },
                            },
                            [
                                "version",
                                "module",
                                "site",
                                "environment",
                                "status",
                                "missing_fields",
                            ],
                        ),
                        "freshness": object_schema(
                            {
                                "status": {
                                    "type": "string",
                                    "enum": ["fresh", "stale", "unknown"],
                                },
                                "processed_at": nullable_text,
                                "reason": {
                                    "type": "string",
                                    "enum": [
                                        "not_recorded",
                                        "invalid_timestamp",
                                        "source_unavailable",
                                        "source_current",
                                        "source_newer_than_index",
                                    ],
                                },
                            },
                            ["status", "processed_at", "reason"],
                        ),
                    },
                    [
                        "document_id",
                        "title",
                        "source",
                        "collection",
                        "domain",
                        "process_code",
                        "process_stage",
                        "document_type",
                        "page_count",
                        "chunk_count",
                        "scope",
                        "freshness",
                    ],
                ),
            },
            "pagination": object_schema(
                {
                    "offset": integer,
                    "limit": integer,
                    "returned": integer,
                    "total": integer,
                },
                ["offset", "limit", "returned", "total"],
            ),
        },
        [
            "catalog_version",
            "workspace",
            "summary",
            "index_health",
            "documents",
            "pagination",
        ],
    )


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_choice(value: Any, name: str, choices: set[str]) -> str | None:
    normalized = _optional_text(value, name)
    if normalized is not None and normalized not in choices:
        raise ToolInputError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return normalized


def _bounded_integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolInputError(f"{name} must be an integer between {minimum} and {maximum}")
    return value
