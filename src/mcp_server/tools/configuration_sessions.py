"""Six coarse-grained MCP tools for durable configuration sessions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from agents import ReviewDecision, SessionStatus
from agents.repositories import SessionRepository, SessionRevisionConflict
from agents.runtime import open_configured_checkpointer
from agents.services import SolutionService, SolutionStateError, ValidationService
from agents.supervisor import RequirementSessionRunner
from core.settings import AgentSettings
from mcp_server.tool_registry import MCPTool, ToolInputError


@dataclass(frozen=True, slots=True)
class ConfigurationSessionApplication:
    runner: RequirementSessionRunner
    repository: SessionRepository
    validation: ValidationService
    solutions: SolutionService
    settings: AgentSettings

    def start(self, goal: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        message = (
            goal
            if not context
            else (
                f"{goal}\nConfirmed context: "
                f"{json.dumps(context, ensure_ascii=False, sort_keys=True)}"
            )
        )

        async def operation():
            async with open_configured_checkpointer(self.settings) as checkpointer:
                return await self.runner.start(message, checkpointer=checkpointer)

        return _workflow_payload(_run(operation()))

    def continue_session(
        self, session_id: str, message: str, expected_revision: int
    ) -> dict[str, Any]:
        self._require_revision(session_id, expected_revision)

        async def operation():
            async with open_configured_checkpointer(self.settings) as checkpointer:
                return await self.runner.continue_session(
                    session_id, message, checkpointer=checkpointer
                )

        return _workflow_payload(_run(operation()))

    def get(self, session_id: str, revision: int | None = None) -> dict[str, Any]:
        record = self.repository.get_revision(session_id, revision)
        return _state_payload(dict(record.state), event_type="draft")

    def validate(self, session_id: str, expected_revision: int) -> dict[str, Any]:
        self._require_revision(session_id, expected_revision)
        record = self.repository.get_revision(session_id, expected_revision)
        state = dict(record.state)
        report = self.validation.validate(
            tasks=list(state.get("configuration_tasks", [])),
            dependency_edges=list(state.get("dependency_edges", [])),
            evidence_registry=list(state.get("evidence_registry", [])),
            bindings=list(state.get("task_evidence_bindings", [])),
            confirmed_context=dict(state.get("confirmed_context", {})),
            invalidated_task_ids=list(state.get("invalidated_task_ids", [])),
        )
        update = {
            "conflicts": [item.to_dict() for item in report.conflicts],
            "validation_findings": [item.to_dict() for item in report.findings],
            "validation_fingerprint": report.fingerprint,
            "status": (
                SessionStatus.PAUSED.value
                if report.blocking
                else SessionStatus.REVIEW_REQUIRED.value
            ),
            "pause_reason": "validation_blocked" if report.blocking else "",
        }
        revision = self.repository.update_revision(
            session_id=session_id,
            expected_revision=expected_revision,
            state_update=update,
            actor="validation",
            reason="explicit_validation",
        )
        return _state_payload(
            dict(revision.state), event_type="interrupt" if report.blocking else "draft"
        )

    def review(
        self,
        session_id: str,
        expected_revision: int,
        decision: str,
        comment: str,
    ) -> dict[str, Any]:
        try:
            review_decision = ReviewDecision(decision)
        except ValueError as exc:
            raise ToolInputError("decision must be revise, reject, or approve") from exc
        revision = self.solutions.review(
            session_id,
            expected_revision=expected_revision,
            decision=review_decision,
            actor="mcp_user",
            comment=comment,
        )
        return _state_payload(
            dict(revision.state),
            event_type="final" if review_decision is not ReviewDecision.REVISE else "draft",
        )

    def export(self, session_id: str, expected_revision: int, format: str) -> dict[str, Any]:
        artifact = self.solutions.export(
            session_id, expected_revision=expected_revision, format=format
        )
        return {
            "event": "final",
            "session_id": session_id,
            "revision": expected_revision,
            "artifact": artifact.to_dict(),
            "markdown": f"Exported `{format}` solution with fingerprint `{artifact.fingerprint}`.",
        }

    def _require_revision(self, session_id: str, expected_revision: int) -> None:
        actual = self.repository.get_session(session_id).current_revision
        if actual != expected_revision:
            raise SessionRevisionConflict(session_id, expected_revision, actual)


class ConfigurationSessionTools:
    def __init__(self, application: ConfigurationSessionApplication) -> None:
        self.application = application

    def definitions(self) -> list[MCPTool]:
        return [
            self._tool("start_configuration_session", self._start, ["goal"], False),
            self._tool(
                "continue_configuration_session",
                self._continue,
                ["session_id", "message", "expected_revision"],
                False,
            ),
            self._tool("get_configuration_session", self._get, ["session_id"], True),
            self._tool(
                "validate_configuration_draft",
                self._validate,
                ["session_id", "expected_revision"],
                False,
            ),
            self._tool(
                "review_configuration_draft",
                self._review,
                ["session_id", "expected_revision", "decision", "comment"],
                False,
            ),
            self._tool(
                "export_configuration_solution",
                self._export,
                ["session_id", "expected_revision", "format"],
                False,
            ),
        ]

    @staticmethod
    def _tool(name, handler, required, read_only) -> MCPTool:
        properties = {
            "goal": {"type": "string", "minLength": 1},
            "context": {"type": "object"},
            "session_id": {"type": "string", "minLength": 1},
            "message": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 1},
            "revision": {"type": "integer", "minimum": 1},
            "decision": {"type": "string", "enum": ["revise", "reject", "approve"]},
            "comment": {"type": "string", "minLength": 1},
            "format": {"type": "string", "enum": ["json", "markdown"]},
        }
        allowed = set(required) | {
            "context"
            if name == "start_configuration_session"
            else "revision"
            if name == "get_configuration_session"
            else ""
        }
        return MCPTool(
            name=name,
            title=name.replace("_", " ").title(),
            description=f"Durable WMS Agent operation: {name}.",
            input_schema={
                "type": "object",
                "properties": {key: value for key, value in properties.items() if key in allowed},
                "required": required,
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "event": {"type": "string"},
                    "session_id": {"type": "string"},
                    "revision": {"type": "integer"},
                    "markdown": {"type": "string"},
                },
                "required": ["event", "session_id", "revision", "markdown"],
            },
            annotations={
                "readOnlyHint": read_only,
                "destructiveHint": False,
                "idempotentHint": read_only
                or name in {"validate_configuration_draft", "export_configuration_solution"},
                "openWorldHint": False,
            },
            handler=handler,
        )

    def _call(self, method, arguments):
        try:
            return _tool_result(method(**arguments))
        except (
            KeyError,
            TypeError,
            ValueError,
            SessionRevisionConflict,
            SolutionStateError,
        ) as exc:
            raise ToolInputError(str(exc)) from exc

    def _start(self, a):
        return self._call(self.application.start, a)

    def _continue(self, a):
        return self._call(self.application.continue_session, a)

    def _get(self, a):
        return self._call(self.application.get, a)

    def _validate(self, a):
        return self._call(self.application.validate, a)

    def _review(self, a):
        return self._call(self.application.review, a)

    def _export(self, a):
        return self._call(self.application.export, a)


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-mcp") as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _workflow_payload(result) -> dict[str, Any]:
    event = "interrupt" if result.interrupts else "final" if not result.next_nodes else "progress"
    return _state_payload(result.state, event_type=event, interrupts=list(result.interrupts))


def _state_payload(state, *, event_type, interrupts=None):
    return {
        "event": event_type,
        "session_id": state["session_id"],
        "revision": int(state["revision"]),
        "status": state.get("status"),
        "state": state,
        "interrupts": interrupts or [],
        "markdown": (
            f"Session `{state['session_id']}` revision {state['revision']} "
            f"is `{state.get('status')}`."
        ),
    }


def _tool_result(payload):
    return {
        "content": [{"type": "text", "text": payload["markdown"]}],
        "structuredContent": payload,
        "isError": False,
    }
