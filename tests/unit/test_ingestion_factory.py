from __future__ import annotations

from dataclasses import replace

from core.settings import load_settings
from ingestion import IngestionPipeline, create_ingestion_pipeline


def test_ingestion_factory_uses_explicit_local_runtime_paths(tmp_path) -> None:
    settings = load_settings()
    settings = replace(
        settings,
        embedding=replace(settings.embedding, cache_dir=tmp_path / "models"),
        ingestion=replace(
            settings.ingestion,
            extract_images=False,
            image_storage=replace(settings.ingestion.image_storage, enabled=False),
        ),
        vector_store=replace(settings.vector_store, persist_path=tmp_path / "chroma"),
        observability=replace(settings.observability, trace_file=tmp_path / "traces.jsonl"),
    )

    pipeline = create_ingestion_pipeline(
        settings,
        source_root=tmp_path / "staging",
        output_root=tmp_path / "processed",
        history_path=tmp_path / "db" / "ingestion_history.db",
        bm25_path=tmp_path / "bm25",
    )

    assert isinstance(pipeline, IngestionPipeline)
    assert pipeline.corpus_processor.integrity.database_path == (
        tmp_path / "db" / "ingestion_history.db"
    )
    assert pipeline.corpus_processor.output_root == tmp_path / "processed"
    assert pipeline.trace_collector is not None
    assert pipeline.trace_collector.path == tmp_path / "traces.jsonl"
