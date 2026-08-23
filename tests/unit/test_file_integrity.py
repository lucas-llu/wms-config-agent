import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


def test_failed_record_is_not_skipped(tmp_path: Path) -> None:
    checker = SQLiteIntegrityChecker(tmp_path / "history.db")

    checker.mark_failed("failed-hash", "invalid document", "broken.pdf")

    assert not checker.should_skip("failed-hash")


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
