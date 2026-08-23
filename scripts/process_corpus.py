"""Build a manifest and incrementally preprocess an authorized local PDF corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.settings import SplitterSettings, load_settings
from ingestion import CorpusManifestBuilder, CorpusProcessor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("../14_system_training"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/corpus/manifest.jsonl"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/corpus/processed"),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/db/corpus_preprocessing.db"),
    )
    parser.add_argument("--chunk-size", type=int)
    parser.add_argument("--chunk-overlap", type=int)
    parser.add_argument("--extract-images", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    splitter_settings = SplitterSettings(
        provider=settings.splitter.provider,
        chunk_size=args.chunk_size or settings.splitter.chunk_size,
        chunk_overlap=(
            args.chunk_overlap
            if args.chunk_overlap is not None
            else settings.splitter.chunk_overlap
        ),
    )

    builder = CorpusManifestBuilder()
    entries = builder.scan(args.source)
    builder.write(entries, args.manifest)
    processor = CorpusProcessor(
        source_root=args.source,
        output_root=args.output,
        database_path=args.database,
        splitter_settings=splitter_settings,
        extract_images=args.extract_images,
    )
    report = processor.process(entries, force=args.force, fail_fast=args.fail_fast)
    result = {
        "manifest": builder.summarize(entries).to_dict(),
        "processing": report.to_dict(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if report.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
