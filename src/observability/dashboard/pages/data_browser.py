"""Read-only document, chunk, metadata, and image browser."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from observability.dashboard.services import DataService, get_dashboard_services


def render(data_service: DataService | None = None) -> None:
    st.title("Data browser")
    st.caption("Read-only view of indexed documents and evidence chunks")
    try:
        data_service = data_service or get_dashboard_services().data
        collections = data_service.list_collections()
    except Exception as exc:
        st.error(f"Knowledge-base data is unavailable: {type(exc).__name__}: {exc}")
        return

    selected_collection = st.selectbox("Collection", ["All collections", *collections])
    collection_filter = None if selected_collection == "All collections" else selected_collection
    try:
        documents = data_service.list_documents(collection_filter)
    except Exception as exc:
        st.error(f"Documents could not be loaded: {type(exc).__name__}: {exc}")
        return
    if not documents:
        st.info("No indexed documents were found for this collection.")
        return

    st.dataframe(
        data_service.document_rows(collection_filter),
        width="stretch",
        hide_index=True,
        column_config={"Source": st.column_config.TextColumn(width="large")},
    )
    labels = {
        document.doc_id: document.title or Path(document.source_path).name for document in documents
    }
    selected_id = st.selectbox(
        "Document details",
        [document.doc_id for document in documents],
        format_func=labels.__getitem__,
    )
    try:
        detail = data_service.get_document_detail(selected_id)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        st.error(f"Document details could not be loaded: {type(exc).__name__}: {exc}")
        return

    st.subheader(labels[selected_id])
    st.caption(detail.document.source_path)
    for chunk in data_service.chunk_rows(detail):
        with st.expander(f"Chunk {chunk['number']} · {chunk['id']}"):
            st.text(chunk["text"] or "[Empty chunk]")
            st.json(chunk["metadata"])

    images = data_service.previewable_images(detail)
    if images:
        st.subheader("Images")
        for image in images:
            st.image(
                str(image["file_path"]),
                caption=f"{image['image_id']} · page {image['page_num'] or 'unknown'}",
            )


if __name__ == "__main__":
    render()
