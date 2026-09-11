from __future__ import annotations

import pytest

from core.settings import load_settings
from mcp_server.tool_registry import MCPTool, ToolInputError, ToolRegistry
from mcp_server.tools.action_catalog import ActionCatalogTool
from mcp_server.tools.agent_capabilities import AgentCapabilitiesTool
from mcp_server.tools.configuration_sessions import ConfigurationSessionTools


def test_action_capability_and_registry_agree_without_executing_handlers():
    registry = ToolRegistry(ConfigurationSessionTools(object()).definitions())
    actions = ActionCatalogTool(registry, "workspace:a")
    registry.register(actions.definition())
    capabilities = AgentCapabilitiesTool(load_settings(), [], registry=registry)
    registry.register(capabilities.definition())
    first = actions.call({})["structuredContent"]
    assert first["workspace_id"] == "workspace:a"
    projected = [
        {k: item[k] for k in ("name", "title", "annotations")} for item in first["actions"]
    ]
    assert projected == capabilities.payload()["tools"]
    assert [item["name"] for item in projected] == [item["name"] for item in registry.definitions()]
    export = next(
        item for item in first["actions"] if item["name"] == "export_configuration_solution"
    )
    assert export["requires_approved_revision"]
    assert "approved_revision" in export["prerequisites"]
    assert export["effect"] == "local_state_change"
    assert actions.definition().annotations["readOnlyHint"]
    with pytest.raises(ToolInputError):
        actions.call({"execute": "export_configuration_solution"})


def test_catalog_reflects_new_registration_and_never_invents_actions():
    registry = ToolRegistry()
    catalog = ActionCatalogTool(registry, "workspace:legacy")
    registry.register(catalog.definition())
    assert len(catalog.call({})["structuredContent"]["actions"]) == 1
    registry.register(
        MCPTool(
            "read_example",
            "Read example",
            "Read",
            {},
            lambda _: pytest.fail("Discovery must not call handlers"),
        )
    )
    result = catalog.call({})
    assert [a["name"] for a in result["structuredContent"]["actions"]] == [
        "get_agent_actions",
        "read_example",
    ]
    assert "environment_inspector" not in str(result)
    assert "apply_configuration" not in str(result)


def test_action_output_validates_against_declared_schema():
    from jsonschema import Draft202012Validator

    registry = ToolRegistry()
    tool = ActionCatalogTool(registry, "workspace:legacy")
    registry.register(tool.definition())
    schema = tool.definition().output_schema
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(tool.call({})["structuredContent"])
