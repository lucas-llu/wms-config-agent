"""Staged PDF ingestion and confirmed document deletion controls."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from observability.dashboard.services import IngestionService, get_ingestion_service


def render(ingestion_service: IngestionService | None = None) -> None:
    st.title("Ingestion")
    st.caption("Upload one PDF into an explicit collection and monitor bounded pipeline progress")
    try:
        ingestion_service = ingestion_service or get_ingestion_service()
    except Exception as exc:
        st.error(f"Ingestion services are unavailable: {type(exc).__name__}: {exc}")
        st.info("Check writable storage paths and provider settings, then reload this page.")
        return

    uploaded = st.file_uploader("PDF", type=["pdf"], accept_multiple_files=False)
    collection = st.text_input(
        "Collection",
        placeholder="for example: inbound-config",
        help="Required. Letters, numbers, dot, dash, and underscore only.",
    )
    force = st.checkbox("Force a complete corpus resynchronization", value=False)
    if st.button("Ingest PDF", type="primary"):
        if uploaded is None:
            st.error("Select a PDF before starting ingestion.")
        else:
            progress_bar = st.progress(0.0, text="Validating upload")
            last_fraction = 0.0

            def show_progress(stage: str, current: int, total: int) -> None:
                nonlocal last_fraction
                update = ingestion_service.bounded_progress(stage, current, total)
                last_fraction = max(last_fraction, update.fraction)
                progress_bar.progress(
                    last_fraction,
                    text=f"{stage}: {update.current}/{update.total}",
                )

            try:
                result = ingestion_service.ingest_pdf(
                    uploaded.name,
                    uploaded.getvalue(),
                    collection,
                    force=force,
                    on_progress=show_progress,
                )
            except Exception as exc:
                progress_bar.empty()
                st.error(f"Ingestion failed: {type(exc).__name__}: {exc}")
            else:
                progress_bar.progress(1.0, text="Ingestion complete")
                st.success(f"Indexed {Path(result.source_path).name} into {result.collection}.")
                columns = st.columns(4)
                columns[0].metric("Chunks", result.indexing.total_chunks)
                columns[1].metric("Dense", result.indexing.vector_count)
                columns[2].metric("Sparse", result.indexing.bm25_count)
                columns[3].metric("Skipped", "Yes" if result.skipped else "No")
                if result.trace_id:
                    st.caption(f"Trace ID: {result.trace_id}")

    st.divider()
    st.subheader("Delete an indexed document")
    st.caption("Deletion removes matching Dense, BM25, image, history, and managed artifacts.")
    try:
        documents = ingestion_service.list_documents()
    except Exception as exc:
        st.error(f"Indexed documents could not be loaded: {type(exc).__name__}: {exc}")
        return
    if not documents:
        st.info("No indexed documents are available for deletion.")
        return

    labels = {
        item.doc_id: f"{item.title or Path(item.source_path).name} · {item.collection}"
        for item in documents
    }
    selected_id = st.selectbox(
        "Document", [item.doc_id for item in documents], format_func=labels.get
    )
    selected = next(item for item in documents if item.doc_id == selected_id)
    phrase = ingestion_service.deletion_phrase(selected)
    st.warning("This operation cannot be undone from the Dashboard.")
    st.code(phrase)
    confirmation = st.text_input("Type the confirmation phrase", key="delete_confirmation")
    if st.button(
        "Delete document",
        disabled=confirmation.strip() != phrase,
        type="secondary",
    ):
        try:
            result = ingestion_service.delete_document(
                selected_id,
                confirmation=confirmation,
            )
        except Exception as exc:
            st.error(f"Deletion failed: {type(exc).__name__}: {exc}")
        else:
            if result.success:
                st.success(
                    "Document deleted: "
                    f"{result.dense_deleted} Dense, {result.sparse_deleted} BM25, "
                    f"{result.images_deleted} images, {result.artifacts_deleted} artifacts."
                )
            else:
                st.error("Deletion was only partially completed: " + "; ".join(result.errors))


if __name__ == "__main__":
    render()
