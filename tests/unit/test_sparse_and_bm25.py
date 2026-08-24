from __future__ import annotations

from core.types import Chunk
from ingestion.embedding import SparseEncoder
from ingestion.storage import BM25Indexer


def _chunk(chunk_id: str, text: str) -> Chunk:
    return Chunk(
        id=chunk_id,
        text=text,
        metadata={"source_path": f"{chunk_id}.pdf"},
        start_offset=0,
        end_offset=len(text),
    )


def test_sparse_encoder_and_bm25_round_trip(tmp_path) -> None:
    chunks = [
        _chunk("putaway", "RF directed putaway putaway configuration"),
        _chunk("outbound", "Outbound staging lane assignment"),
        _chunk("appointment", "Inbound appointment creation"),
    ]
    encodings = SparseEncoder().encode(chunks)
    index = BM25Indexer(tmp_path)
    index.build(encodings)

    assert index.count() == 3
    assert index.query("putaway configuration", top_k=2)[0]["chunk_id"] == "putaway"

    reloaded = BM25Indexer(tmp_path)
    assert reloaded.count() == 3
    assert reloaded.query("appointment", top_k=1)[0]["chunk_id"] == "appointment"


def test_sparse_encoder_includes_enriched_summary_and_tags() -> None:
    chunk = _chunk("policy", "configuration steps")
    chunk.metadata.update(
        {"summary": "Controls allocation behavior", "tags": ["MOCA", "allocation"]}
    )

    encoding = SparseEncoder().encode([chunk])[0]

    assert encoding.term_frequencies["controls"] == 1
    assert encoding.term_frequencies["moca"] == 1
    assert encoding.term_frequencies["allocation"] == 2


def test_remove_document_rebuilds_and_persists_index(tmp_path) -> None:
    index = BM25Indexer(tmp_path)
    index.build(SparseEncoder().encode([_chunk("one", "putaway"), _chunk("two", "outbound")]))

    assert index.remove_document(["one", "missing"]) == 1
    assert index.query("putaway") == []
    assert BM25Indexer(tmp_path).count() == 1
