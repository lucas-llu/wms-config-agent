"""Shared visual theme for the Dashboard pages.

The theme is presentation-only: injecting the stylesheet emits a single
``st.markdown`` element and never touches widget state, which keeps the
page contracts exercised by the test-suite intact.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

_CSS_PATH = Path(__file__).with_name("theme.css")


def apply_global_theme() -> None:
    """Inject the shared dashboard stylesheet into the current page."""
    st.markdown(f"<style>{_CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)
