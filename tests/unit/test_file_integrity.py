import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from libs.loader.file_integrity import SQLiteIntegrityChecker


def test_sha256_is_deterministic_and_success_is_skipped(tmp_path: Path) -> None:
    source = tmp_path / "moca.md"
    source.write_text("MOCA policy configuration", encoding="utf-8")
    checker = SQLiteIntegrityChecker(tmp_path / "db" / "history.db")

    first_hash = checker.compute_sha256(source)
    second_hash = checker.compute_sha256(source)

    assert first_hash == second_hash
    assert not checker.should_skip(first_hash)
    checker.mark_success(first_hash, source, collection="moca")
    assert checker.should_skip(first_hash)


def test_processing_signature_must_match_for_incremental_skip(tmp_path: Path) -> None:
    checker = SQLiteIntegrityChecker(tmp_path / "history.db")
    checker.mark_success(
        "same-file",
        "manual.pdf",
        processing_signature="transform-v1",
    )

    assert checker.should_skip("same-file", processing_signature="transform-v1")
    assert not checker.should_skip("same-file", processing_signature="transform-v2")


def test_failed_record_is_not_skipped(tmp_path: Path) -> None:
    checker = SQLiteIntegrityChecker(tmp_path / "history.db")

    checker.mark_failed("failed-hash", "invalid document", "broken.pdf")

    assert not checker.should_skip("failed-hash")


def test_failed_retry_preserves_existing_lifecycle_artifact_metadata(tmp_path: Path) -> None:
    checker = SQLiteIntegrityChecker(tmp_path / "history.db")
    checker.mark_success(
        "hash",
        "manual.pdf",
        collection="manuals",
        staged_pdf_path="staging/manual.pdf",
        document_artifact_path="processed/documents/manual.json",
        chunk_artifact_paths=["processed/chunks/manual.jsonl"],
    )

    checker.mark_failed("hash", "embedding failed", collection="manuals")

    record = checker.list_processed(status=None, collection="manuals")[0]
    assert record.status == "failed"
    assert record.file_path == "manual.pdf"
    assert record.metadata["staged_pdf_path"] == "staging/manual.pdf"
    assert record.metadata["document_artifact_path"].endswith("manual.json")
    assert record.metadata["chunk_artifact_paths"] == ["processed/chunks/manual.jsonl"]


def test_database_uses_wal_and_accepts_concurrent_writes(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "history.db"
    checker = SQLiteIntegrityChecker(database_path)

    def write(index: int) -> None:
        checker.mark_success(f"hash-{index}", f"document-{index}.pdf")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write, range(12)))

    with sqlite3.connect(database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM ingestion_history").fetchone()[0]

    assert database_path.is_file()
    assert journal_mode.lower() == "wal"
    assert count == 12


def test_concurrent_checker_construction_serializes_schema_initialization(tmp_path: Path) -> None:
    database = tmp_path / "history.db"

    with ThreadPoolExecutor(max_workers=16) as pool:
        checkers = list(pool.map(lambda _: SQLiteIntegrityChecker(database), range(32)))

    assert len(checkers) == 32
    checkers[0].mark_success("hash", "manual.pdf", collection="manuals")
    assert checkers[-1].should_skip("hash", collection="manuals")


def test_list_processed_filters_and_remove_record(tmp_path: Path) -> None:
    checker = SQLiteIntegrityChecker(tmp_path / "history.db")
    checker.mark_success("success", "manual.pdf", collection="manuals", chunk_count=2)
    checker.mark_failed("failed", "parse error", "broken.pdf")

    successes = checker.list_processed(collection="manuals")

    assert len(successes) == 1
    assert successes[0].file_hash == "success"
    assert successes[0].metadata["chunk_count"] == 2
    assert len(checker.list_processed(status=None)) == 2
    assert checker.remove_record(file_hash="success") == 1
    assert checker.remove_record(file_path="missing.pdf") == 0
    assert [record.file_hash for record in checker.list_processed(status=None)] == ["failed"]


def test_same_hash_is_tracked_and_removed_independently_per_collection(tmp_path: Path) -> None:
    checker = SQLiteIntegrityChecker(tmp_path / "history.db")
    checker.mark_success("same", "manual.pdf", collection="manuals")
    checker.mark_success("same", "manual.pdf", collection="training")

    assert checker.should_skip("same", collection="manuals")
    assert checker.should_skip("same", collection="training")
    assert len(checker.list_processed(status=None)) == 2
    assert checker.remove_record(file_hash="same", collection="manuals") == 1
    assert not checker.should_skip("same", collection="manuals")
    assert checker.should_skip("same", collection="training")


def test_legacy_schema_is_migrated_without_losing_history(tmp_path: Path) -> None:
    database = tmp_path / "history.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE ingestion_history (
                file_hash TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                status TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                error_msg TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.execute(
            "INSERT INTO ingestion_history VALUES (?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                "legacy.pdf",
                "success",
                "2026-01-01T00:00:00+00:00",
                None,
                '{"collection": "manuals"}',
            ),
        )
        connection.execute(
            "INSERT INTO ingestion_history VALUES (?, ?, ?, ?, ?, ?)",
            (
                "private-corpus",
                "private.pdf",
                "success",
                "2026-01-02T00:00:00+00:00",
                None,
                "{}",
            ),
        )

    checker = SQLiteIntegrityChecker(database)

    assert checker.should_skip("legacy", collection="manuals")
    assert checker.list_processed(collection="manuals")[0].file_path == "legacy.pdf"
    assert checker.should_skip("private-corpus", collection="wms-system-training")
    assert checker.list_processed(collection="wms-system-training")[0].file_path == "private.pdf"
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ingestion_history)")}
    assert {"record_id", "collection"}.issubset(columns)


def test_read_only_checker_has_no_filesystem_side_effects_and_rejects_writes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "db" / "history.db"
    writable = SQLiteIntegrityChecker(database)
    writable.mark_success("hash", "manual.pdf", collection="manuals")
    before = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    checker = SQLiteIntegrityChecker(database, read_only=True)

    assert checker.should_skip("hash", collection="manuals")
    assert checker.list_processed(collection="manuals")[0].file_hash == "hash"
    with pytest.raises(PermissionError, match="read-only"):
        checker.mark_success("other", "other.pdf")
    with pytest.raises(PermissionError, match="read-only"):
        checker.remove_record(file_hash="hash")
    after = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_read_only_checker_tolerates_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "history.db"

    checker = SQLiteIntegrityChecker(database, read_only=True)

    assert not checker.should_skip("missing")
    assert checker.list_processed() == []
    assert not database.parent.exists()


def test_checker_releases_schema_probe_connection(tmp_path: Path) -> None:
    database = tmp_path / "history.db"
    SQLiteIntegrityChecker(database).mark_success("hash", "manual.pdf", collection="manuals")

    checker = SQLiteIntegrityChecker(database, read_only=True)
    assert checker.list_processed(collection="manuals")

    moved = tmp_path / "history-moved.db"
    database.replace(moved)
    assert moved.is_file()
