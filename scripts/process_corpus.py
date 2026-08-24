"""Build a manifest and incrementally preprocess an authorized local PDF corpus."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from core.settings import SplitterSettings, load_settings
from ingestion import CorpusManifestBuilder, CorpusProcessor
from ingestion.storage import ImageStorage
from ingestion.transform import ChunkRefiner, ImageCaptioner, MetadataEnricher
from libs.llm import BudgetedLLM, LLMFactory


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


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
    parser.add_argument(
        "--enable-llm",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Explicitly enable or disable both text LLM transforms for this run",
    )
    parser.add_argument(
        "--max-documents",
        type=_positive_int,
        help="Process only the first N deterministic manifest entries",
    )
    parser.add_argument(
        "--max-llm-calls",
        type=_positive_int,
        help="Shared logical-call budget across text LLM transforms",
    )
    parser.add_argument(
        "--retry-llm-failures",
        action="store_true",
        help="Retry only chunks recorded with an active LLM transform fallback",
    )
    return parser.parse_args()


def _require_bounded_llm_run(
    *,
    llm_enabled: bool,
    max_documents: int | None,
    max_llm_calls: int | None,
) -> None:
    if llm_enabled and (max_documents is None or max_llm_calls is None):
        raise SystemExit(
            "LLM ingestion requires both --max-documents and --max-llm-calls; "
            "start with an authorized small sample"
        )


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
    selected_entries = entries[: args.max_documents] if args.max_documents else entries
    image_storage = None
    if settings.ingestion.image_storage.enabled:
        try:
            image_storage = ImageStorage(
                settings.ingestion.image_storage.root_path,
                settings.ingestion.image_storage.database_path,
            )
        except (OSError, sqlite3.Error):
            image_storage = None
    refiner_settings = settings.ingestion.chunk_refiner
    enricher_settings = settings.ingestion.metadata_enricher
    if args.enable_llm is not None:
        refiner_settings = replace(refiner_settings, use_llm=args.enable_llm)
        enricher_settings = replace(enricher_settings, use_llm=args.enable_llm)
    llm_enabled = bool(
        (refiner_settings.enabled and refiner_settings.use_llm)
        or (enricher_settings.enabled and enricher_settings.use_llm)
    )
    _require_bounded_llm_run(
        llm_enabled=llm_enabled,
        max_documents=args.max_documents,
        max_llm_calls=args.max_llm_calls,
    )
    llm = LLMFactory.create(settings)
    if args.max_llm_calls is not None:
        llm = BudgetedLLM(llm, args.max_llm_calls)
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
            ChunkRefiner(refiner_settings, llm=llm),
            MetadataEnricher(enricher_settings, llm=llm),
            ImageCaptioner(settings, vision_llm=vision_llm),
        ),
        image_storage=image_storage,
        image_collection=settings.ingestion.image_storage.collection,
    )
    report = processor.process(
        selected_entries,
        force=args.force,
        fail_fast=args.fail_fast,
        retry_llm_failures=args.retry_llm_failures,
    )
    result = {
        "manifest": builder.summarize(entries).to_dict(),
        "selection": {
            "documents": len(selected_entries),
            "llm_enabled": llm_enabled,
            "max_llm_calls": args.max_llm_calls,
            "llm_calls_made": llm.calls_made if isinstance(llm, BudgetedLLM) else None,
        },
        "processing": report.to_dict(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if report.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
