from __future__ import annotations

import errno
from pathlib import Path

import pytest

from libs import atomic_file


def test_atomic_replace_retries_transient_windows_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    original_replace = atomic_file.os.replace
    calls = 0
    delays: list[float] = []

    def transient_replace(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(errno.EACCES, "temporarily locked")
        original_replace(source_path, destination_path)

    monkeypatch.setattr(atomic_file, "_IS_WINDOWS", True)
    monkeypatch.setattr(atomic_file.os, "replace", transient_replace)
    monkeypatch.setattr(atomic_file.time, "sleep", delays.append)

    atomic_file.replace_file_atomically(
        source,
        destination,
        max_attempts=4,
        initial_delay_seconds=0.01,
        max_delay_seconds=0.1,
    )

    assert calls == 3
    assert delays == [0.01, 0.02]
    assert destination.read_text(encoding="utf-8") == "new"
    assert not source.exists()


def test_atomic_replace_does_not_retry_non_windows_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    source.write_text("new", encoding="utf-8")
    calls = 0
    delays: list[float] = []

    def denied_replace(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        del source_path, destination_path
        calls += 1
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(atomic_file, "_IS_WINDOWS", False)
    monkeypatch.setattr(atomic_file.os, "replace", denied_replace)
    monkeypatch.setattr(atomic_file.time, "sleep", delays.append)

    with pytest.raises(PermissionError, match="denied"):
        atomic_file.replace_file_atomically(source, destination)

    assert calls == 1
    assert delays == []


@pytest.mark.parametrize(
    ("max_attempts", "initial_delay_seconds", "max_delay_seconds"),
    [(0, 0.05, 0.5), (1, -0.01, 0.5), (1, 0.05, -0.5)],
)
def test_atomic_replace_rejects_invalid_retry_settings(
    max_attempts: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        atomic_file.replace_file_atomically(
            "source",
            "destination",
            max_attempts=max_attempts,
            initial_delay_seconds=initial_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )
