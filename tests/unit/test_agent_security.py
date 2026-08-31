from __future__ import annotations

from pathlib import Path

from mcp_server.tool_registry import ToolRegistry
from mcp_server.tools import ConfigurationSessionTools


def test_agent_tools_expose_no_destructive_or_open_world_capability() -> None:
    definitions = ToolRegistry(
        ConfigurationSessionTools(object()).definitions()  # type: ignore[arg-type]
    ).definitions()

    assert all(item["annotations"]["destructiveHint"] is False for item in definitions)
    assert all(item["annotations"]["openWorldHint"] is False for item in definitions)
    assert all("environment" not in item["name"] for item in definitions)


def test_agent_business_data_is_gitignored() -> None:
    ignore = Path(".gitignore").read_text(encoding="utf-8")

    assert "data/" in ignore
    assert "*.db" in ignore or "data/" in ignore
