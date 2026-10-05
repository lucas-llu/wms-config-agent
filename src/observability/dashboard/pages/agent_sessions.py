"""Agent session state and audit view."""

from __future__ import annotations

import os

import streamlit as st

from agents.repositories import SessionRepository
from core.settings import load_settings
from ingestion.storage import ImageStorage
from mcp_server.app import create_protocol_handler
from observability.dashboard.services.evidence_images import EvidenceImages
from observability.dashboard.services.workbench_service import WorkbenchService
from observability.dashboard.workbench import render_workbench

config_path = os.environ.get("WMS_CONFIG_PATH", "config/settings.yaml")
settings = load_settings(config_path)


def call_tool(name, arguments):
    # Provider-backed composition is lazy: only explicit user actions need it.
    return create_protocol_handler(settings_path=config_path).registry.call(name, arguments)


try:
    service = WorkbenchService(
        SessionRepository(settings.agent.session_db_path, workspace_id=settings.agent.workspace_id),
        call_tool,
        enabled=settings.agent.enabled,
        checkpoint_path=settings.agent.checkpoint_path,
        evidence_images=EvidenceImages(
            ImageStorage(
                settings.ingestion.image_storage.root_path,
                settings.ingestion.image_storage.database_path,
                read_only=True,
            ),
            [settings.ingestion.image_storage.root_path],
        ),
    )
    render_workbench(service)
except Exception:
    st.error("无法加载工作台。请检查主机 Workspace 和会话存储配置。")
