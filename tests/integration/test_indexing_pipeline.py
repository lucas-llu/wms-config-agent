from __future__ import annotations

import json

import pytest

from core.trace import TraceContext
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
        vector_store=ChromaStore(persist_path=tmp_path / "chroma", collection_name="chunks"),
        bm25_indexer=BM25Indexer(tmp_path / "bm25"),
        batch_size=2,
    )

    first = pipeline.index(chunks, on_progress=lambda *event: progress.append(event))
    second = pipeline.index(chunks)

    assert first.model_trained is True
    assert first.dense_upserted == 4
    assert first.dense_deleted == 0
    assert first.vector_count == first.bm25_count == 4
    assert second.model_trained is False
    assert second.dense_upserted == 0
    assert second.dense_skipped == 4
    assert ("dense_encode", 4, 4) in progress
    assert progress[-1] == ("bm25_build", 4, 4)


def test_force_rebuild_removes_vector_records_not_in_current_corpus(tmp_path) -> None:
    chunks = _chunks()
    pipeline = IndexingPipeline(
        embedding=LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models"),
        vector_store=ChromaStore(persist_path=tmp_path / "chroma", collection_name="chunks"),
        bm25_indexer=BM25Indexer(tmp_path / "bm25"),
        batch_size=2,
    )
    pipeline.index(chunks, force=True)

    replacement = Chunk(
        id="replacement",
        text="Cycle count adjustment configuration",
        metadata={"source_path": "replacement.pdf", "domain": "WMS"},
        start_offset=0,
        end_offset=36,
    )
    report = pipeline.index([*chunks[:3], replacement], force=True)

    assert report.dense_deleted == 1
    assert report.vector_count == report.bm25_count == 4


def test_load_preprocessed_chunks_is_deterministic(tmp_path) -> None:
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    chunks = _chunks()[:2]
    (chunks_dir / "b.jsonl").write_text(json.dumps(chunks[0].to_dict()) + "\n", encoding="utf-8")
    (chunks_dir / "a.jsonl").write_text(json.dumps(chunks[1].to_dict()) + "\n", encoding="utf-8")

    loaded = load_preprocessed_chunks(chunks_dir)

    assert [chunk.id for chunk in loaded] == sorted(chunk.id for chunk in chunks)


def test_local_lsa_dimension_change_replaces_chroma_generation(tmp_path) -> None:
    pipeline = IndexingPipeline(
        embedding=LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models"),
        vector_store=ChromaStore(persist_path=tmp_path / "chroma", collection_name="chunks"),
        bm25_indexer=BM25Indexer(tmp_path / "bm25"),
        batch_size=2,
    )
    pipeline.index(_chunks())
    replacement = [
        Chunk(
            id="cycle-count",
            text="Cycle count adjustment policy",
            metadata={"source_path": "cycle.pdf", "collection": "test"},
            start_offset=0,
            end_offset=29,
        ),
        Chunk(
            id="inventory-hold",
            text="Inventory hold release configuration",
            metadata={"source_path": "hold.pdf", "collection": "test"},
            start_offset=0,
            end_offset=36,
        ),
    ]

    report = pipeline.index(replacement)

    assert pipeline.embedding.actual_dimensions == 1
    assert report.vector_count == report.bm25_count == 2
    assert set(pipeline.vector_store.list_ids()) == {"cycle-count", "inventory-hold"}
    query_vector = pipeline.embedding.embed_query("inventory hold")
    assert len(query_vector) == 1
    assert pipeline.vector_store.query(query_vector, top_k=1)[0]["id"] in {
        "cycle-count",
        "inventory-hold",
    }
    reopened = ChromaStore(persist_path=tmp_path / "chroma", collection_name="chunks")
    assert set(reopened.list_ids()) == {"cycle-count", "inventory-hold"}
    assert not any(
        collection.name.startswith("wms-backup-")
        for collection in reopened.client.list_collections()
    )


def test_bm25_failure_rolls_back_dense_and_prepared_embedding(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = IndexingPipeline(
        embedding=LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models"),
        vector_store=ChromaStore(persist_path=tmp_path / "chroma", collection_name="chunks"),
        bm25_indexer=BM25Indexer(tmp_path / "bm25"),
        batch_size=2,
    )
    original_chunks = _chunks()
    pipeline.index(original_chunks)
    original_ids = set(pipeline.vector_store.list_ids())
    original_signature = pipeline.embedding.signature
    original_model = pipeline.embedding.model_path.read_bytes()
    original_sparse_ids = set(pipeline.bm25_indexer.documents)
    original_build = pipeline.bm25_indexer.build
    failed = False

    def fail_after_replace(encodings) -> None:
        nonlocal failed
        original_build(encodings)
        if not failed:
            failed = True
            raise RuntimeError("injected sparse commit failure")

    monkeypatch.setattr(pipeline.bm25_indexer, "build", fail_after_replace)
    trace = TraceContext("ingestion")
    changed = [
        Chunk(
            id="new-a",
            text="New cycle count configuration",
            metadata={"source_path": "a.pdf", "collection": "test"},
            start_offset=0,
            end_offset=29,
        ),
        Chunk(
            id="new-b",
            text="New inventory hold configuration",
            metadata={"source_path": "b.pdf", "collection": "test"},
            start_offset=0,
            end_offset=32,
        ),
    ]

    with pytest.raises(RuntimeError, match="injected sparse"):
        pipeline.index(changed, trace=trace)

    assert set(pipeline.vector_store.list_ids()) == original_ids
    assert set(pipeline.bm25_indexer.documents) == original_sparse_ids
    assert pipeline.embedding.signature == original_signature
    assert pipeline.embedding.model_path.read_bytes() == original_model
    failure = next(
        stage for stage in trace.to_dict()["stages"] if stage["name"] == "bm25_build_failure"
    )
    assert failure["details"]["rollback"] == {
        "bm25": "restored",
        "embedding": "restored",
    }
    assert not pipeline.vector_store.swap_journal_path.exists()


def test_writer_constructed_before_another_swap_refreshes_canonical_generation(tmp_path) -> None:
    def create_pipeline() -> IndexingPipeline:
        return IndexingPipeline(
            embedding=LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models"),
            vector_store=ChromaStore(
                persist_path=tmp_path / "chroma",
                collection_name="chunks",
            ),
            bm25_indexer=BM25Indexer(tmp_path / "bm25"),
            batch_size=2,
        )

    first_writer = create_pipeline()
    stale_writer = create_pipeline()
    first_writer.index(_chunks())
    replacement = [
        Chunk(
            id="replacement-a",
            text="Replacement cycle count configuration",
            metadata={"source_path": "a.pdf", "collection": "test"},
            start_offset=0,
            end_offset=37,
        ),
        Chunk(
            id="replacement-b",
            text="Replacement inventory hold configuration",
            metadata={"source_path": "b.pdf", "collection": "test"},
            start_offset=0,
            end_offset=40,
        ),
    ]

    report = stale_writer.index(replacement)

    assert report.vector_count == report.bm25_count == 2
    assert set(stale_writer.vector_store.list_ids()) == {
        "replacement-a",
        "replacement-b",
    }


def test_count_validation_failure_rolls_back_before_transaction_backup_is_released(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = IndexingPipeline(
        embedding=LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path / "models"),
        vector_store=ChromaStore(persist_path=tmp_path / "chroma", collection_name="chunks"),
        bm25_indexer=BM25Indexer(tmp_path / "bm25"),
        batch_size=2,
    )
    pipeline.index(_chunks())
    old_ids = set(pipeline.vector_store.list_ids())
    old_sparse = set(pipeline.bm25_indexer.documents)
    old_signature = pipeline.embedding.signature
    replacement = [
        Chunk(
            id="validation-a",
            text="Cycle count adjustment policy for inventory accuracy",
            metadata={"source_path": "a.pdf", "collection": "test"},
            start_offset=0,
            end_offset=52,
        ),
        Chunk(
            id="validation-b",
            text="Outbound dock staging lane assignment for shipping",
            metadata={"source_path": "b.pdf", "collection": "test"},
            start_offset=0,
            end_offset=50,
        ),
    ]
    monkeypatch.setattr(
        pipeline.vector_store,
        "count",
        lambda: (_ for _ in ()).throw(RuntimeError("injected count failure")),
    )
    trace = TraceContext("ingestion")

    with pytest.raises(RuntimeError, match="injected count"):
        pipeline.index(replacement, trace=trace)

    assert set(pipeline.vector_store.list_ids()) == old_ids
    assert set(pipeline.bm25_indexer.documents) == old_sparse
    assert pipeline.embedding.signature == old_signature
    assert any(stage["name"] == "commit_validation_failure" for stage in trace.to_dict()["stages"])
