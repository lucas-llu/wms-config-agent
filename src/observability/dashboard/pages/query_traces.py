"""Query trace search, retrieval diagnostics, fallback state, and latency views."""

from __future__ import annotations

import streamlit as st

from observability.dashboard.services import TraceService, get_dashboard_services


def render(trace_service: TraceService | None = None) -> None:
    st.title("Query traces")
    st.caption("Dense/Sparse retrieval, fusion, rerank fallback, and latency diagnostics")
    search = st.text_input("Search", placeholder="query, collection, or trace ID")
    status_label = st.selectbox("Status", ["All", "ok", "error", "incomplete"])
    try:
        trace_service = trace_service or get_dashboard_services().traces
        result = trace_service.list_traces(
            "query",
            status=None if status_label == "All" else status_label,
            search=search,
        )
    except Exception as exc:
        st.error(f"Query traces are unavailable: {type(exc).__name__}: {exc}")
        return

    if result.malformed_lines:
        st.warning(f"Ignored {result.malformed_lines} malformed or unsupported trace lines.")
    if result.truncated:
        st.info("Showing the bounded tail of the trace file.")
    if not result.records:
        st.info("No query traces match the current filters.")
        return

    st.dataframe(trace_service.summary_rows(result.records), width="stretch", hide_index=True)
    labels = {
        record.trace_id: (
            f"{record.started_at} · {record.status} · "
            f"{record.attributes.get('query', record.trace_id)}"
        )
        for record in result.records
    }
    selected_id = st.selectbox("Trace details", list(labels), format_func=labels.get)
    selected = next(record for record in result.records if record.trace_id == selected_id)
    diagnostics = trace_service.query_diagnostics(selected)
    columns = st.columns(5)
    columns[0].metric("Total latency", f"{selected.total_elapsed_ms:.3f} ms")
    columns[1].metric("Dense", diagnostics["dense_count"])
    columns[2].metric("Sparse", diagnostics["sparse_count"])
    columns[3].metric("Final", diagnostics["final_count"])
    columns[4].metric("Rerank fallback", "Yes" if diagnostics["rerank_fallback"] else "No")

    if diagnostics["failures"]:
        st.warning("Failed stages: " + ", ".join(diagnostics["failures"]))
    stage_rows = trace_service.stage_rows(selected)
    if stage_rows:
        st.bar_chart(stage_rows, x="Stage", y="Duration (ms)")
        st.dataframe(stage_rows, width="stretch", hide_index=True)
    rankings = diagnostics["rankings"]
    if rankings:
        st.subheader("Privacy-safe ranking snapshots")
        for route in ("dense", "sparse", "fused", "final"):
            values = rankings.get(route)
            if isinstance(values, list) and values:
                with st.expander(route.capitalize()):
                    st.dataframe(values, width="stretch", hide_index=True)


if __name__ == "__main__":
    render()
