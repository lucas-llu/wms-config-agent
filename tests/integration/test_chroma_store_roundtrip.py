from __future__ import annotations

import pytest

from core.types import ChunkRecord
from libs.vector_store import ChromaStore

pytestmark = pytest.mark.integration


def _record(chunk_id: str, vector: list[float], *, domain: str = "Inbound") -> ChunkRecord:
    return ChunkRecord(
        id=chunk_id,
        text=f"Text for {chunk_id}",
        metadata={
            "source_path": f"{chunk_id}.pdf",
            "domain": domain,
            "pages": [1, 2],
        },
        dense_vector=vector,
    )


def test_chroma_upsert_query_filter_and_get(tmp_path) -> None:
    store = ChromaStore(persist_path=tmp_path, collection_name="test_chunks")
    records = [
        _record("putaway", [1.0, 0.0, 0.0]),
        _record("appointment", [0.8, 0.2, 0.0]),
        _record("outbound", [0.0, 1.0, 0.0], domain="Outbound"),
    ]

    store.upsert(records)
    store.upsert(records)

    assert store.count() == 3
    assert store.query([1.0, 0.0, 0.0], top_k=1)[0]["id"] == "putaway"
    assert store.query([0.0, 0.0, 0.0], top_k=3) == []
    filtered = store.query([0.0, 1.0, 0.0], top_k=3, filters={"domain": "Inbound"})
    assert {item["id"] for item in filtered} == {"putaway", "appointment"}
    restored = store.get_by_ids(["outbound", "missing"])
    assert restored[0]["metadata"]["pages"] == [1, 2]

    inbound = store.get_by_metadata({"domain": "Inbound"})
    assert {item["id"] for item in inbound} == {"putaway", "appointment"}
    stats = store.get_collection_stats()
    assert stats["chunk_count"] == 3
    assert stats["document_count"] == 3
    assert store.delete_by_metadata({"domain": "Outbound"}) == 1
    assert store.count() == 2
