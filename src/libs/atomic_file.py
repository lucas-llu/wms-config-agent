"""Cross-platform atomic file replacement with Windows lock retries."""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

_IS_WINDOWS = os.name == "nt"
_RETRYABLE_WINDOWS_ERRORS = {5, 32, 33}
_RETRYABLE_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM}


def replace_file_atomically(
    source: str | Path,
    destination: str | Path,
    *,
    max_attempts: int = 8,
    initial_delay_seconds: float = 0.05,
    max_delay_seconds: float = 0.5,
) -> None:
    """Replace ``destination`` while tolerating transient Windows file locks.

    Antivirus, indexers, and recently closed native readers can briefly hold a file
    between the temporary write and ``os.replace``.  Retrying only the Windows sharing
    and permission errors preserves atomic replacement without hiding missing paths,
    invalid destinations, or persistent failures.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if initial_delay_seconds < 0 or max_delay_seconds < 0:
        raise ValueError("retry delays must not be negative")

    source_path = Path(source)
    destination_path = Path(destination)
    delay = min(initial_delay_seconds, max_delay_seconds)

    for attempt in range(max_attempts):
        try:
            os.replace(source_path, destination_path)
            return
        except OSError as exc:
            if attempt == max_attempts - 1 or not _is_retryable_windows_error(exc):
                raise
            time.sleep(delay)
            delay = min(max_delay_seconds, max(initial_delay_seconds, delay * 2))


def _is_retryable_windows_error(error: OSError) -> bool:
    if not _IS_WINDOWS:
        return False
    return (
        isinstance(error, PermissionError)
        or getattr(error, "winerror", None) in _RETRYABLE_WINDOWS_ERRORS
        or error.errno in _RETRYABLE_ERRNOS
    )
