"""Vectorize preprocessed WMS chunks and persist Chroma and BM25 indexes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.settings import load_settings
from ingestion import IndexingPipeline, load_preprocessed_chunks
from ingestion.storage import BM25Indexer
from libs.embedding import EmbeddingFactory
from libs.vector_store import VectorStoreFactory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunks", type=Path, default=Path("data/corpus/processed/chunks")
    )
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--bm25-path", type=Path, default=Path("data/db/bm25"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    settings = load_settings(args.settings)
    chunks = load_preprocessed_chunks(args.chunks)
    pipeline = IndexingPipeline(
        embedding=EmbeddingFactory.create(settings),
        vector_store=VectorStoreFactory.create(settings),
        bm25_indexer=BM25Indexer(args.bm25_path),
        batch_size=settings.embedding.batch_size,
    )
    def show_progress(stage: str, current: int, total: int) -> None:
        print(f"[{stage}] {current}/{total}", file=sys.stderr)

    report = pipeline.index(
        chunks,
        force=args.force,
        on_progress=None if args.quiet else show_progress,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
