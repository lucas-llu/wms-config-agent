"""Ingestion trace history and stage-duration diagnostics."""

from __future__ import annotations

import streamlit as st

from observability.dashboard.services import TraceService, get_dashboard_services


def render(trace_service: TraceService | None = None) -> None:
    st.title("Ingestion traces")
    st.caption("Recent ingestion outcomes and privacy-safe pipeline stage timings")
    search = st.text_input("Search", placeholder="source name, collection, or trace ID")
    status_label = st.selectbox("Status", ["All", "ok", "error", "incomplete"])
    try:
        trace_service = trace_service or get_dashboard_services().traces
        result = trace_service.list_traces(
            "ingestion",
            status=None if status_label == "All" else status_label,
            search=search,
        )
    except Exception as exc:
        st.error(f"Ingestion traces are unavailable: {type(exc).__name__}: {exc}")
        return

    _show_reader_diagnostics(result.malformed_lines, result.truncated)
    if not result.records:
        st.info("No ingestion traces match the current filters.")
        return
    st.dataframe(trace_service.summary_rows(result.records), width="stretch", hide_index=True)
    labels = {
        record.trace_id: (
            f"{record.started_at} · {record.status} · "
            f"{record.attributes.get('source_name', record.trace_id)}"
        )
        for record in result.records
    }
    selected_id = st.selectbox("Trace details", list(labels), format_func=labels.get)
    selected = next(record for record in result.records if record.trace_id == selected_id)
    columns = st.columns(3)
    columns[0].metric("Status", selected.status)
    columns[1].metric("Total latency", f"{selected.total_elapsed_ms:.3f} ms")
    columns[2].metric("Stages", len(selected.stages))
    stage_rows = trace_service.stage_rows(selected)
    if stage_rows:
        st.bar_chart(stage_rows, x="Stage", y="Duration (ms)")
        st.dataframe(stage_rows, width="stretch", hide_index=True)
    if selected.error:
        st.error(f"Failure category: {selected.error}")


def _show_reader_diagnostics(malformed_lines: int, truncated: bool) -> None:
    if malformed_lines:
        st.warning(f"Ignored {malformed_lines} malformed or unsupported trace lines.")
    if truncated:
        st.info("Showing the bounded tail of the trace file.")


if __name__ == "__main__":
    render()
