from __future__ import annotations

from mcp_server.tool_registry import ToolRegistry
from mcp_server.tools import ConfigurationSessionTools


class FakeApplication:
    def __init__(self) -> None:
        self.revisions = {"session:one": 1, "session:two": 1}

    @staticmethod
    def _payload(session_id, revision, event="draft"):
        return {
            "event": event,
            "session_id": session_id,
            "revision": revision,
            "markdown": f"{session_id}:{revision}",
        }

    def start(self, goal, context=None):
        del context
        session_id = "session:one" if "one" in goal else "session:two"
        return self._payload(session_id, self.revisions[session_id], "interrupt")

    def continue_session(self, session_id, message, expected_revision):
        del message
        if self.revisions[session_id] != expected_revision:
            raise ValueError("revision conflict")
        self.revisions[session_id] += 1
        return self._payload(session_id, self.revisions[session_id])

    def get(self, session_id, revision=None):
        return self._payload(session_id, revision or self.revisions[session_id])

    def validate(self, session_id, expected_revision):
        return self.continue_session(session_id, "validate", expected_revision)

    def review(self, session_id, expected_revision, decision, comment):
        del decision, comment
        return self.continue_session(session_id, "review", expected_revision)

    def export(self, session_id, expected_revision, format):
        del format
        return self._payload(session_id, expected_revision, "final")


def _registry():
    return ToolRegistry(ConfigurationSessionTools(FakeApplication()).definitions())


def test_day8_tool_schema_snapshot_and_annotations() -> None:
    definitions = _registry().definitions()

    assert [item["name"] for item in definitions] == [
        "continue_configuration_session",
        "export_configuration_solution",
        "get_configuration_session",
        "review_configuration_draft",
        "start_configuration_session",
        "validate_configuration_draft",
    ]
    assert all(item["inputSchema"]["additionalProperties"] is False for item in definitions)
    assert all("outputSchema" in item for item in definitions)
    get_tool = next(item for item in definitions if item["name"] == "get_configuration_session")
    assert get_tool["annotations"]["readOnlyHint"] is True


def test_two_sessions_remain_isolated_and_errors_are_structured() -> None:
    registry = _registry()
    first = registry.call("start_configuration_session", {"goal": "one"})
    second = registry.call("start_configuration_session", {"goal": "two"})
    continued = registry.call(
        "continue_configuration_session",
        {"session_id": "session:one", "message": "next", "expected_revision": 1},
    )
    untouched = registry.call("get_configuration_session", {"session_id": "session:two"})
    stale = registry.call(
        "continue_configuration_session",
        {"session_id": "session:one", "message": "stale", "expected_revision": 1},
    )

    assert first["structuredContent"]["event"] == "interrupt"
    assert second["structuredContent"]["session_id"] == "session:two"
    assert continued["structuredContent"]["revision"] == 2
    assert untouched["structuredContent"]["revision"] == 1
    assert stale["isError"] is True
    assert "revision conflict" in stale["structuredContent"]["error"]
