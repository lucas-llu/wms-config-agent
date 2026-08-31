"""Streamlit entry point for the local WMS Config Agent Dashboard."""

from __future__ import annotations

from pathlib import Path

import streamlit as st


def main() -> None:
    st.set_page_config(page_title="WMS Config Agent", page_icon="📦", layout="wide")
    page_root = Path(__file__).parent / "pages"
    navigation = st.navigation(
        [
            st.Page(page_root / "overview.py", title="Overview", icon="🏠", default=True),
            st.Page(page_root / "data_browser.py", title="Data browser", icon="📚"),
            st.Page(page_root / "ingestion_manager.py", title="Ingestion", icon="📥"),
            st.Page(page_root / "ingestion_traces.py", title="Ingestion traces", icon="🧭"),
            st.Page(page_root / "query_traces.py", title="Query traces", icon="🔎"),
            st.Page(page_root / "agent_sessions.py", title="Agent sessions", icon="🤖"),
            st.Page(page_root / "evaluation.py", title="Evaluation", icon="📊"),
        ]
    )
    navigation.run()


if __name__ == "__main__":
    main()
