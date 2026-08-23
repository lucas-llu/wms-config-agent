"""Build a manifest and incrementally preprocess an authorized local PDF corpus."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from core.settings import SplitterSettings, load_settings
from ingestion import CorpusManifestBuilder, CorpusProcessor
from ingestion.storage import ImageStorage
from ingestion.transform import ChunkRefiner, ImageCaptioner, MetadataEnricher
from libs.llm import LLMFactory


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
    parser.add_argument(
        "--extract-images",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override ingestion.extract_images from settings.yaml",
    )
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
    image_storage = None
    if settings.ingestion.image_storage.enabled:
        try:
            image_storage = ImageStorage(
                settings.ingestion.image_storage.root_path,
                settings.ingestion.image_storage.database_path,
            )
        except (OSError, sqlite3.Error):
            image_storage = None
    llm = LLMFactory.create(settings)
    vision_llm = LLMFactory.create_vision_llm(settings)
    processor = CorpusProcessor(
        source_root=args.source,
        output_root=args.output,
        database_path=args.database,
        splitter_settings=splitter_settings,
        extract_images=(
            settings.ingestion.extract_images
            if args.extract_images is None
            else args.extract_images
        ),
        transforms=(
            ChunkRefiner(settings, llm=llm),
            MetadataEnricher(settings, llm=llm),
            ImageCaptioner(settings, vision_llm=vision_llm),
        ),
        image_storage=image_storage,
        image_collection=settings.ingestion.image_storage.collection,
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
