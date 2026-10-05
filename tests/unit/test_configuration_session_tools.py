from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from agents import ExportArtifact
from core.settings import load_settings
from mcp_server.tool_registry import ToolRegistry
from mcp_server.tools import ConfigurationSessionApplication, ConfigurationSessionTools


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


class FakeRepository:
    def __init__(self) -> None:
        self.state = {
            "session_id": "session:real",
            "revision": 1,
            "status": "validating",
            "user_goal": "goal",
            "configuration_tasks": [],
            "dependency_edges": [],
            "evidence_registry": [],
            "task_evidence_bindings": [],
            "confirmed_context": {},
            "invalidated_task_ids": [],
        }

    def get_session(self, session_id):
        return SimpleNamespace(session_id=session_id, current_revision=self.state["revision"])

    def get_revision(self, session_id, revision=None):
        del session_id, revision
        return SimpleNamespace(state=dict(self.state))

    def update_revision(self, **kwargs):
        self.state.update(kwargs["state_update"])
        self.state["revision"] += 1
        return SimpleNamespace(state=dict(self.state))


class FakeRunner:
    async def start(self, message, *, checkpointer):
        del message, checkpointer
        return SimpleNamespace(
            state={"session_id": "session:real", "revision": 1, "status": "paused"},
            interrupts=({"kind": "question"},),
            next_nodes=("await",),
        )

    async def continue_session(self, session_id, message, *, checkpointer):
        del message, checkpointer
        return SimpleNamespace(
            state={"session_id": session_id, "revision": 2, "status": "review_required"},
            interrupts=(),
            next_nodes=(),
        )


class FakeValidation:
    @staticmethod
    def validate(**kwargs):
        del kwargs
        return SimpleNamespace(
            conflicts=(), findings=(), fingerprint="validation:one", blocking=False
        )


class FakeSolutions:
    @staticmethod
    def review(session_id, **kwargs):
        del kwargs
        return SimpleNamespace(
            state={"session_id": session_id, "revision": 2, "status": "approved"}
        )

    @staticmethod
    def export(session_id, **kwargs):
        del session_id, kwargs
        return ExportArtifact(format="json", path="exports/solution.json", fingerprint="a" * 64)


def test_application_facade_runs_all_six_paths(tmp_path) -> None:
    settings = replace(
        load_settings().agent,
        checkpoint_path=tmp_path / "checkpoints.db",
        session_db_path=tmp_path / "sessions.db",
    )
    repository = FakeRepository()
    app = ConfigurationSessionApplication(
        runner=FakeRunner(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        validation=FakeValidation(),  # type: ignore[arg-type]
        solutions=FakeSolutions(),  # type: ignore[arg-type]
        settings=settings,
    )

    assert app.start("goal")["event"] == "interrupt"
    assert app.continue_session("session:real", "next", 1)["event"] == "final"
    assert app.get("session:real")["status"] == "validating"
    assert app.validate("session:real", 1)["status"] == "review_required"
    repository.state.update({"revision": 1, "status": "review_required"})
    assert app.review("session:real", 1, "approve", "ok")["status"] == "approved"
    assert app.export("session:real", 1, "json")["artifact"]["fingerprint"] == "a" * 64
