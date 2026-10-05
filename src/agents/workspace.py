"""Immutable local workspace policies; this is scope isolation, not network identity."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class WorkspaceScopeError(ValueError):
    """A request is outside the selected workspace policy."""


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    name: str
    collections: tuple[str, ...]
    modules: tuple[str, ...]
    sites: tuple[str, ...]
    environments: tuple[str, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"workspace:[A-Za-z0-9_-]{1,64}", self.workspace_id):
            raise WorkspaceScopeError("Invalid workspace identifier")
        if not isinstance(self.name, str) or not self.name.strip():
            raise WorkspaceScopeError("Workspace name is required")
        for field in ("collections", "modules", "sites", "environments"):
            values = getattr(self, field)
            if (
                not isinstance(values, tuple)
                or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in values)
                or len(values) != len(set(values))
            ):
                raise WorkspaceScopeError(f"Invalid workspace {field}")
            if not values and self.workspace_id != "workspace:legacy":
                raise WorkspaceScopeError(f"Workspace {field} must be an explicit allowlist")

    def check(self, field: str, values: list[str]) -> None:
        allowed = getattr(self, field)
        if self.workspace_id == "workspace:legacy":
            return
        if any(not isinstance(v, str) or v not in allowed for v in values):
            raise WorkspaceScopeError(f"Requested {field} outside workspace scope")

    def validate_state(self, state: dict[str, Any]) -> None:
        if state.get("workspace_id", self.workspace_id) != self.workspace_id:
            raise WorkspaceScopeError("Session workspace cannot be changed")
        context = state.get("confirmed_context", {})
        if not isinstance(context, dict):
            raise WorkspaceScopeError("Invalid confirmed context")
        for source, target in (
            ("modules", "modules"),
            ("site", "sites"),
            ("environment", "environments"),
            ("collection", "collections"),
        ):
            value = context.get(source)
            if value is not None:
                values = value if isinstance(value, list) else [value]
                self.check(target, values)
        for task in state.get("configuration_tasks", []):
            self.check("modules", [task.get("module")])
        for evidence in state.get("evidence_registry", []):
            if self.workspace_id != "workspace:legacy":
                for source, target in (
                    ("module", "modules"),
                    ("site", "sites"),
                    ("environment", "environments"),
                    ("collection", "collections"),
                ):
                    self.check(target, [evidence.get(source)])

    def filters(self, requested: dict[str, Any]) -> dict[str, Any]:
        result = dict(requested)
        if self.workspace_id == "workspace:legacy":
            return result
        for source, target in (
            ("module", "modules"),
            ("site", "sites"),
            ("environment", "environments"),
            ("collection", "collections"),
        ):
            allowed = getattr(self, target)
            value = result.get(source)
            if value is None:
                if len(allowed) != 1:
                    raise WorkspaceScopeError(f"Select one allowed {source} before retrieval")
                result[source] = allowed[0]
            self.check(target, [result[source]])
        return result

    def permits_metadata(self, metadata: dict[str, Any]) -> bool:
        if self.workspace_id == "workspace:legacy":
            return True
        return all(
            metadata.get(key) in getattr(self, field)
            for key, field in (
                ("collection", "collections"),
                ("module", "modules"),
                ("site", "sites"),
                ("environment", "environments"),
            )
        )


def load_workspace(connection: sqlite3.Connection, workspace_id: str) -> Workspace:
    row = connection.execute(
        "SELECT policy_json FROM workspaces WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    if row is None:
        raise WorkspaceScopeError("Workspace does not exist")
    payload = json.loads(row[0])
    for key in ("collections", "modules", "sites", "environments"):
        payload[key] = tuple(payload[key])
    return Workspace(**payload)


class WorkspaceService:
    """Local administrative provisioning; policies are immutable after creation."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def create(self, workspace: Workspace) -> None:
        if workspace.workspace_id == "workspace:legacy":
            raise WorkspaceScopeError("Legacy workspace is reserved")
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id, policy_json) VALUES (?, ?)",
                (workspace.workspace_id, json.dumps(asdict(workspace), sort_keys=True)),
            )

    def get(self, workspace_id: str) -> Workspace:
        with sqlite3.connect(self.database_path) as connection:
            return load_workspace(connection, workspace_id)
