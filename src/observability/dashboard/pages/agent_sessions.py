"""Agent session state and audit view."""

from __future__ import annotations

import os

import streamlit as st

from agents.repositories import SessionRepository
from core.settings import load_settings
from observability.dashboard.services import AgentSessionService

settings = load_settings(os.environ.get("WMS_CONFIG_PATH", "config/settings.yaml"))
service = AgentSessionService(SessionRepository(settings.agent.session_db_path))

st.title("Agent Sessions")
rows = service.list_rows()
st.dataframe(rows, use_container_width=True, hide_index=True)
if rows:
    session_id = st.selectbox("Session", [item["Session"] for item in rows])
    detail = service.detail(session_id)
    st.subheader("Current state")
    st.json({key: detail[key] for key in ("revision", "status", "confirmed_context")})
    st.subheader("Task DAG")
    st.json({"tasks": detail["tasks"], "edges": detail["dependency_edges"]})
    st.subheader("Evidence coverage")
    st.json(detail["evidence_bindings"])
    st.subheader("Conflicts and findings")
    st.json({"conflicts": detail["conflicts"], "findings": detail["findings"]})
    st.subheader("Interrupt and approval history")
    st.json({"pause_reason": detail["pause_reason"], "approvals": detail["approvals"]})
