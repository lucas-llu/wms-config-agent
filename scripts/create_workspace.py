"""Provision an immutable local workspace; select it in agent.workspace_id afterwards."""

from __future__ import annotations

import argparse

from agents.repositories import SessionRepository
from agents.workspace import Workspace, WorkspaceService
from core.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--id", required=True)
    parser.add_argument("--name", required=True)
    for field in ("collections", "modules", "sites", "environments"):
        parser.add_argument(f"--{field}", nargs="+", required=True)
    args = parser.parse_args()
    settings = load_settings(args.settings)
    workspace = Workspace(
        args.id,
        args.name,
        tuple(args.collections),
        tuple(args.modules),
        tuple(args.sites),
        tuple(args.environments),
    )
    SessionRepository(settings.agent.session_db_path)
    WorkspaceService(settings.agent.session_db_path).create(workspace)
    print(f"Created {workspace.workspace_id}; set agent.workspace_id and restart the host.")


if __name__ == "__main__":
    main()
