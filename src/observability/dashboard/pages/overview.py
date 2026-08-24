"""System overview page."""

from __future__ import annotations

import streamlit as st

from observability.dashboard.services import ConfigService, DataService, get_dashboard_services


def render(
    config_service: ConfigService | None = None,
    data_service: DataService | None = None,
) -> None:
    st.title("WMS Config Agent")
    st.caption("Local knowledge-base configuration and index health")
    try:
        if config_service is None or data_service is None:
            services = get_dashboard_services()
            config_service = config_service or services.config
            data_service = data_service or services.data
        summary = config_service.project_summary()
        stats = data_service.get_collection_stats()
    except Exception as exc:
        st.error(f"Dashboard services are unavailable: {type(exc).__name__}: {exc}")
        st.info("Check the settings and local index paths, then reload this page.")
        return

    st.subheader(summary["name"])
    st.caption(f"Environment: {summary['environment']} · Settings: {summary['settings_path']}")
    columns = st.columns(4)
    columns[0].metric("Documents", stats.document_count)
    columns[1].metric("Dense chunks", stats.chunk_count)
    columns[2].metric("Sparse chunks", stats.sparse_chunk_count)
    columns[3].metric("Images", stats.image_count)

    if stats.chunk_count != stats.sparse_chunk_count:
        st.warning(
            "Dense and sparse index counts differ. Rebuild the indexes before production use."
        )
    else:
        st.success("Dense and sparse indexes are aligned.")

    st.subheader("Components")
    st.dataframe(
        [component.to_dict() for component in config_service.components()],
        width="stretch",
        hide_index=True,
    )


if __name__ == "__main__":
    render()
