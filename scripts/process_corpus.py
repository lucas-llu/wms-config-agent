"""Build a manifest and incrementally preprocess an authorized local PDF corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from core.settings import SplitterSettings, load_settings
from ingestion import CorpusManifestBuilder, CorpusProcessor
from ingestion.storage import ImageStorage, LifecycleLock
from ingestion.transform import ChunkRefiner, ImageCaptioner, MetadataEnricher
from libs.llm import BudgetedLLM, LLMFactory
from libs.loader import SQLiteIntegrityChecker
from libs.sqlite_snapshot import connect_sqlite_snapshot

_DEFAULT_HISTORY_DATABASE = Path("data/db/ingestion_history.db")
_LEGACY_HISTORY_DATABASE = Path("data/db/corpus_preprocessing.db")
_LEGACY_HISTORY_COLUMNS = {
    "file_hash",
    "file_path",
    "status",
    "processed_at",
    "error_msg",
    "metadata_json",
}


@dataclass(frozen=True, slots=True)
class HistoryMigrationReport:
    source_found: bool
    rows_read: int
    rows_inserted: int

    def to_dict(self) -> dict[str, bool | int]:
        return asdict(self)


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
        default=_DEFAULT_HISTORY_DATABASE,
    )
    parser.add_argument(
        "--legacy-database",
        type=Path,
        help=(
            "Override the legacy preprocessing history source. The default legacy database is "
            "migrated automatically only when --database uses the unified default."
        ),
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


def migrate_legacy_history(
    legacy_path: str | Path,
    target_path: str | Path,
    *,
    default_collection: str = "wms-system-training",
) -> HistoryMigrationReport:
    """Copy legacy preprocessing history without modifying it or replacing target rows."""
    source = Path(legacy_path)
    target = Path(target_path)
    if source.resolve() == target.resolve() or not source.is_file():
        return HistoryMigrationReport(source_found=source.is_file(), rows_read=0, rows_inserted=0)
    collection = default_collection.strip()
    if not collection:
        raise ValueError("default_collection must not be empty")

    rows = _read_legacy_history(source, default_collection=collection)
    if not rows:
        return HistoryMigrationReport(source_found=True, rows_read=0, rows_inserted=0)

    # Reuse the production initializer so an existing legacy target schema is upgraded first.
    SQLiteIntegrityChecker(target)
    connection = sqlite3.connect(target, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        before = connection.total_changes
        connection.executemany(
            """
            INSERT OR IGNORE INTO ingestion_history (
                record_id, file_hash, collection, file_path, status,
                processed_at, error_msg, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        inserted = connection.total_changes - before
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return HistoryMigrationReport(
        source_found=True,
        rows_read=len(rows),
        rows_inserted=inserted,
    )


def _read_legacy_history(
    source: Path,
    *,
    default_collection: str,
) -> list[tuple[str, str, str, str, str, str, str | None, str]]:
    connection = connect_sqlite_snapshot(source)
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(ingestion_history)").fetchall()
        }
        missing = sorted(_LEGACY_HISTORY_COLUMNS - columns)
        if missing:
            raise ValueError(
                "Legacy history has an unsupported schema; missing columns: " + ", ".join(missing)
            )
        has_collection = "collection" in columns
        selection = "file_hash, file_path, status, processed_at, error_msg, metadata_json" + (
            ", collection" if has_collection else ""
        )
        raw_rows = connection.execute(
            f"SELECT {selection} FROM ingestion_history ORDER BY processed_at, file_hash"
        ).fetchall()
    finally:
        connection.close()

    migrated: list[tuple[str, str, str, str, str, str, str | None, str]] = []
    for row in raw_rows:
        file_hash = str(row[0]).strip()
        file_path = str(row[1])
        status = str(row[2])
        processed_at = str(row[3]).strip()
        error_msg = str(row[4]) if row[4] is not None else None
        metadata_json = str(row[5]) if row[5] is not None else "{}"
        metadata = _metadata_mapping(metadata_json)
        row_collection = row[6] if has_collection else metadata.get("collection")
        collection = str(row_collection).strip() if row_collection is not None else ""
        collection = collection or default_collection
        if not file_hash or not processed_at or status not in {"success", "failed"}:
            raise ValueError("Legacy history contains an invalid required value")
        record_id = hashlib.sha256(f"{file_hash}\0{collection}".encode()).hexdigest()
        migrated.append(
            (
                record_id,
                file_hash,
                collection,
                file_path,
                status,
                processed_at,
                error_msg,
                metadata_json,
            )
        )
    return migrated


def _metadata_mapping(value: str) -> dict[str, Any]:
    try:
        metadata = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


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
    with LifecycleLock.for_database(args.database).lease():
        _process_locked(args, settings)


def _process_locked(args: argparse.Namespace, settings: Any) -> None:
    legacy_database = args.legacy_database
    if legacy_database is None and args.database.resolve() == _DEFAULT_HISTORY_DATABASE.resolve():
        legacy_database = _LEGACY_HISTORY_DATABASE
    migration = (
        migrate_legacy_history(
            legacy_database,
            args.database,
            default_collection=settings.ingestion.image_storage.collection,
        )
        if legacy_database is not None
        else HistoryMigrationReport(False, 0, 0)
    )
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
        "history_migration": migration.to_dict(),
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
