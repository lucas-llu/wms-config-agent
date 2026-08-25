"""Ingest one WMS PDF or index preprocessed chunks into Chroma and BM25."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.settings import Settings, load_settings
from core.trace import TraceCollector
from ingestion import IndexingPipeline, create_ingestion_pipeline, load_preprocessed_chunks
from ingestion.storage import BM25Indexer, LifecycleLock
from libs.embedding import EmbeddingFactory
from libs.vector_store import VectorStoreFactory

_DEFAULT_CHUNKS_PATH = Path("data/corpus/processed/chunks")


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--path",
        type=Path,
        help="Preprocess and index one PDF through the complete ingestion pipeline",
    )
    source.add_argument(
        "--chunks",
        type=Path,
        help=f"Index preprocessed JSONL chunks (default: {_DEFAULT_CHUNKS_PATH})",
    )
    parser.add_argument(
        "--collection",
        help="Required collection name for --path; not applicable to preprocessed chunks",
    )
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--bm25-path", type=Path, default=Path("data/db/bm25"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/corpus/processed"),
        help="Document and Chunk artifact root for --path",
    )
    parser.add_argument(
        "--history-path",
        type=Path,
        default=Path("data/db/ingestion_history.db"),
        help="Unified ingestion history database for --path",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(arguments)
    if args.path is not None:
        if args.collection is None or not args.collection.strip():
            parser.error("--collection is required and must not be empty with --path")
        if args.path.suffix.casefold() != ".pdf":
            parser.error("--path must reference a PDF file")
        if not args.path.is_file():
            parser.error(f"--path does not exist or is not a file: {args.path}")
        args.collection = args.collection.strip()
    elif args.collection is not None:
        parser.error("--collection is only valid with --path")
    return args


def main(arguments: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(arguments)
    settings = load_settings(args.settings)
    result = _run(args, settings)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def _run(args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    if args.path is not None:
        return _ingest_pdf(args, settings)
    return _index_preprocessed_chunks(args, settings)


def _show_progress(stage: str, current: int, total: int) -> None:
    print(f"[{stage}] {current}/{total}", file=sys.stderr)


def _ingest_pdf(args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    source_path = args.path.resolve()
    pipeline = create_ingestion_pipeline(
        settings,
        source_root=source_path.parent,
        output_root=args.output_root,
        history_path=args.history_path,
        bm25_path=args.bm25_path,
    )
    report = pipeline.run(
        source_path,
        collection=args.collection,
        on_progress=None if args.quiet else _show_progress,
        force=args.force,
    )
    return report.to_dict()


def _index_preprocessed_chunks(args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    lifecycle_lock = LifecycleLock.for_database(args.history_path)
    with lifecycle_lock.lease():
        chunks = load_preprocessed_chunks(args.chunks or _DEFAULT_CHUNKS_PATH)
        pipeline = IndexingPipeline(
            embedding=EmbeddingFactory.create(settings),
            vector_store=VectorStoreFactory.create(settings),
            bm25_indexer=BM25Indexer(args.bm25_path),
            batch_size=settings.embedding.batch_size,
        )

        collector = TraceCollector(
            settings.observability.trace_file, enabled=settings.observability.enabled
        )
        trace = collector.start("ingestion", {"chunk_count": len(chunks), "force": args.force})
        try:
            report = pipeline.index(
                chunks,
                force=args.force,
                on_progress=None if args.quiet else _show_progress,
                trace=trace,
            )
            if trace:
                trace.finish()
        except Exception as exc:
            if trace:
                trace.finish(status="error", error=type(exc).__name__)
            raise
        finally:
            collector.collect(trace)
    return report.to_dict()


if __name__ == "__main__":
    main()
