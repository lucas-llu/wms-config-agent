"""Side-effect-free SQLite snapshots for local management reads."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_SQLITE_HEADER = b"SQLite format 3\x00"


def connect_sqlite_snapshot(
    database_path: str | Path,
    *,
    attempts: int = 100,
) -> sqlite3.Connection:
    """Load one stable, checkpointed database image into an isolated memory connection.

    A normal read-only connection can create WAL/SHM sidecars, while ``immutable=1`` can expose
    uncommitted or torn pages during a concurrent writer. This reader waits for active rollback or
    WAL files, verifies the source identity before and after the copy, then queries only the
    in-memory snapshot.
    """
    path = Path(database_path)
    last_error: Exception | None = None
    for attempt in range(attempts):
        if _has_active_writer_sidecar(path):
            _backoff(attempt)
            continue
        before = _signature(path)
        if before is None:
            raise FileNotFoundError(path)
        try:
            payload = bytearray(path.read_bytes())
        except OSError as exc:
            last_error = exc
            _backoff(attempt)
            continue
        after = _signature(path)
        if before != after or _has_active_writer_sidecar(path):
            _backoff(attempt)
            continue
        if len(payload) < 100 or payload[:16] != _SQLITE_HEADER:
            last_error = sqlite3.DatabaseError(f"Invalid SQLite database image: {path}")
            _backoff(attempt)
            continue

        # A checkpointed WAL database still advertises WAL in header bytes 18/19. The detached
        # in-memory image has no WAL file, so switch only the copied header to rollback mode.
        if payload[18] == 2:
            payload[18] = 1
        if payload[19] == 2:
            payload[19] = 1
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(bytes(payload))
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA schema_version").fetchone()
        except sqlite3.DatabaseError as exc:
            connection.close()
            last_error = exc
            _backoff(attempt)
            continue
        return connection

    message = f"Could not capture a stable SQLite snapshot: {path}"
    if last_error is not None:
        raise RuntimeError(message) from last_error
    raise TimeoutError(message)


def _signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        stats = path.stat()
    except FileNotFoundError:
        return None
    return stats.st_dev, stats.st_ino, stats.st_mtime_ns, stats.st_ctime_ns, stats.st_size


def _has_active_writer_sidecar(path: Path) -> bool:
    for suffix in ("-journal", "-wal"):
        sidecar = Path(f"{path}{suffix}")
        try:
            if sidecar.stat().st_size > 0:
                return True
        except FileNotFoundError:
            continue
    return False


def _backoff(attempt: int) -> None:
    time.sleep(min(0.002 * (attempt + 1), 0.05))
