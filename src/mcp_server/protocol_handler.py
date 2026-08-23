"""Version-aware MCP JSON-RPC protocol handling for stdio transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_server.tool_registry import ToolRegistry

MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_PROTOCOL_VERSIONS = (MODERN_PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS)


@dataclass(frozen=True, slots=True)
class JSONRPCError(Exception):
    code: int
    message: str
    data: dict[str, Any] | None = None


class ProtocolHandler:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        server_name: str = "wms-config-agent",
        server_version: str = "0.1.0",
    ) -> None:
        self.registry = registry
        self.server_info = {
            "name": server_name,
            "title": "WMS Configuration Knowledge Agent",
            "version": server_version,
        }
        self.instructions = (
            "Use the read-only tools to retrieve cited WMS/JDA MOCA evidence. "
            "Do not infer configuration steps that are absent from returned sources."
        )

    def handle(self, message: Any) -> dict[str, Any] | None:
        request_id: str | int | None = None
        modern = False
        try:
            request = self._validate_request(message)
            request_id = request.get("id")
            method = request["method"]
            params = request.get("params", {})
            modern = method == "server/discover" or self._is_modern(params)
            if modern:
                self._validate_modern_version(params, method)

            if "id" not in request:
                self._handle_notification(method)
                return None
            result = self._dispatch(method, params, modern=modern)
            if modern:
                result = self._modern_result(result)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JSONRPCError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": request_id, "error": error}
        except Exception:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "Internal error"},
            }

    def parse_error(self) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        }

    def _dispatch(
        self, method: str, params: dict[str, Any], *, modern: bool
    ) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "server/discover":
            return {
                "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": self.instructions,
                "ttlMs": 300_000,
                "cacheScope": "public",
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            result: dict[str, Any] = {"tools": self.registry.definitions()}
            if modern:
                result.update({"ttlMs": 300_000, "cacheScope": "public"})
            return result
        if method == "tools/call":
            return self._call_tool(params)
        raise JSONRPCError(-32601, f"Method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise JSONRPCError(-32602, "initialize requires protocolVersion")
        protocol_version = (
            requested if requested in LEGACY_PROTOCOL_VERSIONS else LEGACY_PROTOCOL_VERSIONS[0]
        )
        return {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self.server_info,
            "instructions": self.instructions,
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            raise JSONRPCError(-32602, "tools/call requires a non-empty name")
        if not isinstance(arguments, dict):
            raise JSONRPCError(-32602, "tools/call arguments must be an object")
        try:
            return self.registry.call(name, arguments)
        except KeyError as exc:
            raise JSONRPCError(-32601, str(exc)) from exc

    @staticmethod
    def _validate_request(message: Any) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise JSONRPCError(-32600, "Invalid Request")
        if message.get("jsonrpc") != "2.0":
            raise JSONRPCError(-32600, "Invalid Request: jsonrpc must be 2.0")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise JSONRPCError(-32600, "Invalid Request: method is required")
        if "id" in message and (
            isinstance(message["id"], bool)
            or not isinstance(message["id"], str | int | type(None))
        ):
            raise JSONRPCError(-32600, "Invalid Request: id must be string or number")
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise JSONRPCError(-32602, "params must be an object")
        return message

    @staticmethod
    def _handle_notification(method: str) -> None:
        if method not in {
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/progress",
        }:
            return

    @staticmethod
    def _is_modern(params: dict[str, Any]) -> bool:
        metadata = params.get("_meta", {})
        return isinstance(metadata, dict) and (
            metadata.get("io.modelcontextprotocol/protocolVersion")
            == MODERN_PROTOCOL_VERSION
        )

    @staticmethod
    def _validate_modern_version(params: dict[str, Any], method: str) -> None:
        if method == "server/discover":
            return
        metadata = params.get("_meta", {})
        requested = (
            metadata.get("io.modelcontextprotocol/protocolVersion")
            if isinstance(metadata, dict)
            else None
        )
        if requested != MODERN_PROTOCOL_VERSION:
            raise JSONRPCError(
                -32022,
                "Unsupported protocol version",
                {"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": requested},
            )

    def _modern_result(self, result: dict[str, Any]) -> dict[str, Any]:
        modern = dict(result)
        modern.setdefault("resultType", "complete")
        metadata = dict(modern.get("_meta", {}))
        metadata["io.modelcontextprotocol/serverInfo"] = self.server_info
        modern["_meta"] = metadata
        return modern
