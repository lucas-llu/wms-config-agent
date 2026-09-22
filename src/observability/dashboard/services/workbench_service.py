"""Workspace-bound workbench read model and explicit tool dispatch."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import PureWindowsPath
from typing import Any

from agents.repositories import SessionRepository
from observability.dashboard.services.agent_session_service import AgentSessionService

_TOOLS = {
    "continue": "continue_configuration_session",
    "validate": "validate_configuration_draft",
    "review": "review_configuration_draft",
    "export": "export_configuration_solution",
    "feedback": "record_configuration_feedback",
    "summary": "get_configuration_feedback_summary",
}
_NEXT = {
    "created": "会话已创建，尚无已完成的 Agent 回合。",
    "paused": "查看待补充问题或证据缺口，再继续对话。",
    "review_required": "检查草稿和证据，然后明确批准、要求修改或拒绝。",
    "approved": "此版本已批准，可以导出；不会写入 WMS。",
    "rejected": "此方案已拒绝，请新建会话描述新的目标。",
    "cancelled": "会话已取消，仅供查看。",
    "failed": "处理失败，请查看问题记录或新建会话。",
}


class WorkbenchService(AgentSessionService):
    def __init__(self, repository: SessionRepository, call_tool: Callable, *, enabled: bool):
        super().__init__(repository)
        self.call_tool = call_tool
        self.enabled = enabled

    def view(self, session_id: str, revision: int) -> dict[str, Any]:
        record = self.repository.get_revision(session_id, revision)
        current = self.repository.get_session(session_id)
        state = record.state
        tasks = state.get("configuration_tasks", [])
        return {
            "revision": record.revision,
            "current_revision": current.current_revision,
            "status": record.status.value,
            "next_step": (
                "已回复，可以继续提问或描述配置目标。"
                if state.get("pause_reason") == "question_answered"
                else _NEXT.get(record.status.value, "处理中；刷新查看最新版本。")
            ),
            "context": state.get("confirmed_context", {}),
            "questions": state.get("open_questions", []),
            "tasks": tasks,
            "dag": task_graph(tasks, state.get("dependency_edges", [])),
            "evidence": [
                {
                    **{
                        key: item.get(key)
                        for key in (
                            "evidence_id",
                            "excerpt",
                            "page_start",
                            "product_version",
                            "module",
                        )
                    },
                    "source": safe_source(item.get("source")),
                }
                for item in state.get("evidence_registry", [])
            ],
            "bindings": state.get("task_evidence_bindings", []),
            "findings": state.get("validation_findings", []),
            "conflicts": state.get("conflicts", []),
            "turns": [
                {"role": item.role, "message": item.message, "revision": item.revision}
                for item in self.repository.list_turns(session_id)
                if item.revision <= revision and item.role in {"user", "assistant"}
            ],
            "approvals": [
                {
                    "revision": item.revision,
                    "decision": item.decision.value,
                    "comment": item.comment,
                    "created_at": item.created_at,
                }
                for item in self.repository.list_approvals(session_id)
                if item.revision <= revision
            ],
        }

    def start(self, goal: str) -> dict[str, Any]:
        if not self.enabled or not goal.strip():
            raise ValueError("Agent must be enabled and a goal supplied")
        return self.call_tool("start_configuration_session", {"goal": goal})

    def act(self, action: str, session_id: str, revision: int, **fields) -> dict[str, Any]:
        if not self.enabled or action not in _TOOLS:
            raise ValueError("Action unavailable")
        allowed = {
            "continue": {"message"},
            "review": {"decision", "comment"},
            "export": {"format"},
            "feedback": {"kind", "reason"},
        }
        if set(fields) - allowed.get(action, set()):
            raise ValueError("Unsupported action fields")
        self.repository.get_revision(session_id, revision)
        if (
            action not in {"feedback", "summary"}
            and self.repository.get_session(session_id).current_revision != revision
        ):
            raise ValueError("Selected revision is stale; refresh before changing the session")
        revision_key = "revision" if action in {"feedback", "summary"} else "expected_revision"
        return self.call_tool(
            _TOOLS[action], {**fields, "session_id": session_id, revision_key: revision}
        )


def task_graph(tasks: list[dict], edges: list[dict]) -> str:
    """Generate DOT using internal node identifiers and escaped labels only."""
    nodes = {item["task_id"]: f"n{index}" for index, item in enumerate(tasks)}
    lines = ["digraph tasks {", "rankdir=LR;"]
    for item in tasks:
        label = json.dumps(str(item.get("title", item["task_id"])), ensure_ascii=False)
        lines.append(f"{nodes[item['task_id']]} [label={label}];")
    for edge in edges:
        source = nodes.get(edge.get("upstream_task_id"))
        target = nodes.get(edge.get("downstream_task_id"))
        if source and target:
            lines.append(f"{source} -> {target};")
    return "\n".join([*lines, "}"])


def safe_source(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "来源未知"
    path = PureWindowsPath(value)
    if path.drive or path.root or ".." in path.parts or ":" in value:
        return "本地来源路径已隐藏"
    return value
