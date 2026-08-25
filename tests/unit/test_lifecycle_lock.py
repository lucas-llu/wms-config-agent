from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from ingestion.storage import LifecycleLock


def test_live_owner_is_not_reclaimed_from_old_mtime(tmp_path: Path) -> None:
    lock = LifecycleLock(tmp_path / "lifecycle.lock", timeout_seconds=0.08)
    owner = lock.lease().acquire()
    old = time.time() - 24 * 60 * 60
    os.utime(owner.path, (old, old))
    try:
        with pytest.raises(TimeoutError, match="Timed out"):
            lock.lease().acquire()
        assert owner.path.is_file()
    finally:
        owner.release()


def test_independent_leases_serialize_concurrent_singleton_calls(tmp_path: Path) -> None:
    lock = LifecycleLock(tmp_path / "lifecycle.lock", timeout_seconds=1.0)
    owner = lock.lease().acquire()
    acquired = threading.Event()

    def contend() -> None:
        with lock.lease():
            acquired.set()

    worker = threading.Thread(target=contend)
    worker.start()
    try:
        assert not acquired.wait(0.08)
    finally:
        owner.release()
    worker.join(timeout=1.0)

    assert acquired.is_set()
    assert not lock.path.exists()


def test_partial_owner_token_is_treated_as_active_until_timeout(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"
    path.write_bytes(b"")

    with pytest.raises(TimeoutError, match="Timed out"):
        LifecycleLock(
            path,
            timeout_seconds=0.06,
            poll_interval_seconds=0.01,
        ).acquire()

    assert path.is_file()
