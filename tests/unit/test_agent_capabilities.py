from __future__ import annotations

from mcp_server.tool_registry import MCPTool, ToolInputError
from mcp_server.tools import AgentCapabilitiesTool
from core.settings import load_settings


def _registered_tool() -> MCPTool:
    return MCPTool(
        name="query_wms_knowledge",
        title="Query WMS Knowledge",
        description="Read-only query",
        input_schema={"type": "object"},
        handler=lambda arguments: arguments,
    )


def test_capability_payload_is_versioned_scoped_and_secret_free(monkeypatch) -> None:
    monkeypatch.setenv("WMS_LLM_API_KEY", "secret-provider-value")
    tool = AgentCapabilitiesTool(load_settings(), [_registered_tool()])

    payload = tool.payload()

    assert payload["schema_version"] == 1
    assert payload["product"]["version"] == "0.1.0"
    assert payload["transport"] == {
        "kind": "stdio",
        "authentication": "host_process",
        "network_exposed": False,
        "protocol_versions": payload["transport"]["protocol_versions"],
    }
    assert payload["features"] == {
        "agent_enabled": False,
        "approval_required": True,
        "environment_inspector_enabled": False,
        "real_wms_write_enabled": False,
        "multi_tenant": False,
    }
    assert payload["provider"]["credentials_available"] is True
    assert "secret-provider-value" not in str(payload)
    assert "api_key" not in str(payload).casefold()
    assert payload["knowledge"]["modules"] == ["appointment", "inbound", "integration"]
    assert [item["name"] for item in payload["tools"]] == [
        "get_agent_capabilities",
        "query_wms_knowledge",
    ]


def test_capability_schema_is_strict_and_tool_is_read_only() -> None:
    definition = AgentCapabilitiesTool(load_settings(), [_registered_tool()]).definition()
    schema = definition.output_schema

    assert definition.input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert schema is not None
    assert schema["additionalProperties"] is False
    assert schema["properties"]["features"]["additionalProperties"] is False
    assert schema["properties"]["tools"]["items"]["additionalProperties"] is False
    assert definition.annotations == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def test_capability_tool_rejects_arguments_and_returns_markdown_fallback() -> None:
    tool = AgentCapabilitiesTool(load_settings(), [_registered_tool()])

    result = tool.call({})

    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert result["structuredContent"]["exports"]["formats"] == ["json", "markdown"]
    try:
        tool.call({"unexpected": True})
    except ToolInputError as exc:
        assert "accepts no arguments" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("capability tool accepted unexpected arguments")
