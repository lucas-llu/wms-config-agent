import argparse
import hashlib
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from libs.loader import SQLiteIntegrityChecker
from scripts.process_corpus import (
    _positive_int,
    _require_bounded_llm_run,
    migrate_legacy_history,
    parse_args,
)


def _create_legacy_history(
    path: Path,
    *,
    file_hash: str = "legacy-hash",
    metadata: dict[str, object] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE ingestion_history (
                file_hash TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                status TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                error_msg TEXT,
                metadata_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO ingestion_history VALUES (?, ?, ?, ?, ?, ?)",
            (
                file_hash,
                "private/manual.pdf",
                "success",
                "2026-08-20T12:00:00+00:00",
                None,
                json.dumps(metadata or {"chunk_count": 7}, sort_keys=True),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_positive_int_rejects_zero() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="greater than 0"):
        _positive_int("0")


def test_llm_ingestion_requires_both_safety_bounds() -> None:
    with pytest.raises(SystemExit, match="--max-documents and --max-llm-calls"):
        _require_bounded_llm_run(
            llm_enabled=True,
            max_documents=2,
            max_llm_calls=None,
        )


def test_local_rule_run_does_not_require_llm_bounds() -> None:
    _require_bounded_llm_run(
        llm_enabled=False,
        max_documents=None,
        max_llm_calls=None,
    )


def test_default_history_database_matches_dashboard_contract(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["process_corpus.py"])

    args = parse_args()

    assert args.database == Path("data/db/ingestion_history.db")


def test_legacy_history_migration_is_readonly_and_idempotent(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy" / "corpus_preprocessing.db"
    target = tmp_path / "unified" / "ingestion_history.db"
    _create_legacy_history(legacy)
    before_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()
    before_mtime = legacy.stat().st_mtime_ns

    first = migrate_legacy_history(legacy, target)
    second = migrate_legacy_history(legacy, target)

    assert first.source_found is True
    assert first.rows_read == 1
    assert first.rows_inserted == 1
    assert second.rows_inserted == 0
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before_hash
    assert legacy.stat().st_mtime_ns == before_mtime
    records = SQLiteIntegrityChecker(target, read_only=True).list_processed(status=None)
    assert len(records) == 1
    assert records[0].collection == "wms-system-training"
    assert records[0].processed_at == "2026-08-20T12:00:00+00:00"
    assert records[0].metadata == {"chunk_count": 7}


def test_legacy_history_migration_does_not_ignore_committed_wal_rows(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "target.db"
    connection = sqlite3.connect(legacy, check_same_thread=False)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        """
        CREATE TABLE ingestion_history (
            file_hash TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            status TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            error_msg TEXT,
            metadata_json TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute(
        "INSERT INTO ingestion_history VALUES (?, ?, ?, ?, ?, ?)",
        (
            "wal-hash",
            "private/wal.pdf",
            "success",
            "2026-08-21T12:00:00+00:00",
            None,
            '{"chunk_count": 3}',
        ),
    )
    connection.commit()
    wal_path = Path(f"{legacy}-wal")
    assert wal_path.stat().st_size > 0

    def checkpoint_committed_row() -> None:
        time.sleep(0.05)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()

    checkpoint = threading.Thread(target=checkpoint_committed_row)
    checkpoint.start()
    try:
        report = migrate_legacy_history(legacy, target)
    finally:
        checkpoint.join(timeout=5)

    assert not checkpoint.is_alive()
    assert report.rows_inserted == 1
    records = SQLiteIntegrityChecker(target, read_only=True).list_processed(status=None)
    assert [record.file_hash for record in records] == ["wal-hash"]


def test_legacy_history_migration_preserves_existing_target_record(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "target.db"
    _create_legacy_history(legacy, metadata={"origin": "legacy"})
    target_history = SQLiteIntegrityChecker(target)
    target_history.mark_success(
        "legacy-hash",
        "current/manual.pdf",
        collection="wms-system-training",
        origin="current",
    )

    report = migrate_legacy_history(legacy, target)

    assert report.rows_inserted == 0
    records = SQLiteIntegrityChecker(target, read_only=True).list_processed(status=None)
    assert len(records) == 1
    assert records[0].file_path == "current/manual.pdf"
    assert records[0].metadata["origin"] == "current"


def test_legacy_history_migration_upgrades_and_preserves_legacy_target_schema(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "target.db"
    _create_legacy_history(legacy, file_hash="source-hash")
    _create_legacy_history(
        target,
        file_hash="target-hash",
        metadata={"origin": "existing-target"},
    )

    report = migrate_legacy_history(legacy, target)

    assert report.rows_inserted == 1
    records = SQLiteIntegrityChecker(target, read_only=True).list_processed(status=None)
    assert {record.file_hash for record in records} == {"source-hash", "target-hash"}
    existing = next(record for record in records if record.file_hash == "target-hash")
    assert existing.metadata["origin"] == "existing-target"


def test_legacy_history_migration_uses_collection_metadata(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "target.db"
    _create_legacy_history(legacy, metadata={"collection": "customer-a"})

    migrate_legacy_history(legacy, target)

    records = SQLiteIntegrityChecker(target, read_only=True).list_processed(status=None)
    assert records[0].collection == "customer-a"


def test_missing_or_invalid_legacy_history_does_not_initialize_target(tmp_path: Path) -> None:
    target = tmp_path / "target.db"

    missing = migrate_legacy_history(tmp_path / "missing.db", target)

    assert missing.source_found is False
    assert not target.exists()

    invalid = tmp_path / "invalid.db"
    connection = sqlite3.connect(invalid)
    connection.execute("CREATE TABLE ingestion_history (file_hash TEXT)")
    connection.close()
    with pytest.raises(ValueError, match="unsupported schema"):
        migrate_legacy_history(invalid, target)
    assert not target.exists()
