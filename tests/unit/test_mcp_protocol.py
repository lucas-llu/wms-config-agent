from __future__ import annotations

import io
import json

from mcp_server.protocol_handler import MODERN_PROTOCOL_VERSION, ProtocolHandler
from mcp_server.server import serve_stdio
from mcp_server.tool_registry import MCPTool, ToolRegistry


def _handler() -> ProtocolHandler:
    tool = MCPTool(
        name="echo",
        title="Echo",
        description="Echo a value.",
        input_schema={"type": "object"},
        handler=lambda arguments: {
            "content": [{"type": "text", "text": str(arguments.get("value", ""))}],
            "structuredContent": {"value": arguments.get("value")},
            "isError": False,
        },
    )
    return ProtocolHandler(ToolRegistry([tool]))


def test_legacy_initialize_and_tool_call() -> None:
    handler = _handler()

    initialized = handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    called = handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"value": "ok"}},
        }
    )

    assert initialized is not None
    assert initialized["result"]["protocolVersion"] == "2025-06-18"
    assert called is not None
    assert called["result"]["structuredContent"] == {"value": "ok"}


def test_modern_discovery_and_list_include_required_metadata() -> None:
    handler = _handler()
    discovered = handler.handle(
        {"jsonrpc": "2.0", "id": "d", "method": "server/discover", "params": {}}
    )
    listed = handler.handle(
        {
            "jsonrpc": "2.0",
            "id": "l",
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION
                }
            },
        }
    )

    assert discovered is not None
    assert MODERN_PROTOCOL_VERSION in discovered["result"]["supportedVersions"]
    assert discovered["result"]["resultType"] == "complete"
    assert listed is not None
    assert listed["result"]["tools"][0]["name"] == "echo"
    assert listed["result"]["resultType"] == "complete"
    assert "io.modelcontextprotocol/serverInfo" in listed["result"]["_meta"]


def test_protocol_errors_do_not_leak_exception_details() -> None:
    handler = _handler()

    missing = handler.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "unknown", "params": {}}
    )
    invalid = handler.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": []}
    )

    assert missing is not None and missing["error"]["code"] == -32601
    assert invalid is not None and invalid["error"]["code"] == -32602
    assert "traceback" not in json.dumps([missing, invalid]).lower()


def test_stdio_uses_one_json_message_per_line_and_skips_notifications() -> None:
    output = io.StringIO()
    serve_stdio(
        _handler(),
        [
            "not-json\n",
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
            '{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
        ],
        output,
    )

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 2
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["result"] == {}
