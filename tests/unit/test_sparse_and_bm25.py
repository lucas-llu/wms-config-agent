from __future__ import annotations

from pathlib import Path

import pytest

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


def test_stale_instance_refreshes_before_mutation_and_preserves_external_upsert(tmp_path) -> None:
    first = BM25Indexer(tmp_path)
    first.build(SparseEncoder().encode([_chunk("one", "putaway"), _chunk("two", "outbound")]))
    stale = BM25Indexer(tmp_path)

    first.upsert(SparseEncoder().encode([_chunk("three", "cycle count")]))
    assert stale.count() == 3

    assert stale.remove_document(["one"]) == 1

    reloaded = BM25Indexer(tmp_path)
    assert reloaded.count() == 2
    assert reloaded.query("cycle count", top_k=1)[0]["chunk_id"] == "three"


def test_load_retries_when_atomic_replace_happens_between_read_and_stat(
    tmp_path, monkeypatch
) -> None:
    index = BM25Indexer(tmp_path)
    index.build(SparseEncoder().encode([_chunk("one", "putaway")]))
    stale = BM25Indexer(tmp_path)
    original_read_text = Path.read_text
    replaced = False

    def racing_read(path: Path, *args, **kwargs) -> str:
        nonlocal replaced
        payload = original_read_text(path, *args, **kwargs)
        if path == stale.index_path and not replaced:
            replaced = True
            index.upsert(SparseEncoder().encode([_chunk("two", "allocation")]))
        return payload

    monkeypatch.setattr(Path, "read_text", racing_read)

    stale.load()

    assert stale.count() == 2
    assert stale.query("allocation", top_k=1)[0]["chunk_id"] == "two"


def test_metadata_removal_and_collection_counts_are_document_scoped(tmp_path) -> None:
    first = _chunk("manuals-chunk", "putaway")
    first.metadata.update({"collection": "manuals", "source_path": "shared.pdf"})
    second = _chunk("training-chunk", "allocation")
    second.metadata.update({"collection": "training", "source_path": "shared.pdf"})
    index = BM25Indexer(tmp_path)
    index.build(SparseEncoder().encode([first, second]))

    assert index.count(collection="manuals") == 1
    assert index.count(collection="training") == 1
    assert (
        index.remove_document(
            metadata_filters={"collection": "manuals", "source_path": "shared.pdf"}
        )
        == 1
    )
    assert index.query("putaway") == []
    assert index.query("allocation", top_k=1)[0]["chunk_id"] == "training-chunk"


def test_collection_count_accepts_legacy_ids_without_metadata(tmp_path) -> None:
    index = BM25Indexer(tmp_path)
    index.build(SparseEncoder().encode([_chunk("legacy", "putaway")]))

    assert index.count(collection="manuals") == 0
    assert index.count(collection="manuals", chunk_ids=["legacy"]) == 1


def test_read_only_indexer_reads_without_initializing_or_mutating_storage(tmp_path) -> None:
    missing_path = tmp_path / "missing"
    missing = BM25Indexer(missing_path, read_only=True)

    assert missing.count() == 0
    assert not missing_path.exists()

    writable = BM25Indexer(tmp_path / "index")
    encodings = SparseEncoder().encode([_chunk("one", "putaway")])
    writable.build(encodings)
    before = writable.index_path.read_bytes()
    read_only = BM25Indexer(tmp_path / "index", read_only=True)

    assert read_only.query("putaway", top_k=1)[0]["chunk_id"] == "one"
    for mutation in (
        lambda: read_only.build(encodings),
        lambda: read_only.upsert(encodings),
        read_only.save,
        lambda: read_only.remove_document(["one"]),
    ):
        with pytest.raises(PermissionError, match="read-only"):
            mutation()
    assert writable.index_path.read_bytes() == before
