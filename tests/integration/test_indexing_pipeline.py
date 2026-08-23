from __future__ import annotations

import json

import pytest

from core.types import Chunk
from ingestion import IndexingPipeline, load_preprocessed_chunks
from ingestion.storage import BM25Indexer
from libs.embedding import LocalLSAEmbedding
from libs.vector_store import ChromaStore

pytestmark = pytest.mark.integration


def _chunks() -> list[Chunk]:
    texts = {
        "putaway": "RF directed putaway configuration and movement",
        "outbound": "Outbound staging lane and dock assignment",
        "appointment": "Inbound appointment creation and schedules",
        "policy": "MOCA policy configuration and warehouse execution",
    }
    return [
        Chunk(
            id=chunk_id,
            text=text,
            metadata={"source_path": f"{chunk_id}.pdf", "domain": "WMS"},
            start_offset=0,
            end_offset=len(text),
        )
        for chunk_id, text in texts.items()
    ]


def test_indexing_pipeline_is_incremental(tmp_path) -> None:
    chunks = _chunks()
    progress: list[tuple[str, int, int]] = []
    pipeline = IndexingPipeline(
        embedding=LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models"),
        vector_store=ChromaStore(
            persist_path=tmp_path / "chroma", collection_name="chunks"
        ),
        bm25_indexer=BM25Indexer(tmp_path / "bm25"),
        batch_size=2,
    )

    first = pipeline.index(chunks, on_progress=lambda *event: progress.append(event))
    second = pipeline.index(chunks)

    assert first.model_trained is True
    assert first.dense_upserted == 4
    assert first.vector_count == first.bm25_count == 4
    assert second.model_trained is False
    assert second.dense_upserted == 0
    assert second.dense_skipped == 4
    assert ("dense_encode", 4, 4) in progress
    assert progress[-1] == ("bm25_build", 4, 4)


def test_load_preprocessed_chunks_is_deterministic(tmp_path) -> None:
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    chunks = _chunks()[:2]
    (chunks_dir / "b.jsonl").write_text(
        json.dumps(chunks[0].to_dict()) + "\n", encoding="utf-8"
    )
    (chunks_dir / "a.jsonl").write_text(
        json.dumps(chunks[1].to_dict()) + "\n", encoding="utf-8"
    )

    loaded = load_preprocessed_chunks(chunks_dir)

    assert [chunk.id for chunk in loaded] == sorted(chunk.id for chunk in chunks)
