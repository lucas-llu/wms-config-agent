"""Cross-process lock for document lifecycle and full-corpus resynchronization."""

from __future__ import annotations

import os
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType


class LifecycleLock(AbstractContextManager["LifecycleLock"]):
    """Serialize history snapshots and coordinated artifact/index mutations.

    Ingestion and document deletion must construct this lock with the same path.  The
    recommended location is next to ``ingestion_history.db`` so every process derives
    the same lock without depending on its current working directory.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than 0")
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._descriptor: int | None = None
        self._token: str | None = None

    @classmethod
    def for_database(
        cls,
        database_path: str | Path,
        **kwargs: float,
    ) -> LifecycleLock:
        """Create the canonical lifecycle lock next to an ingestion-history database."""

        database = Path(database_path)
        return cls(database.parent / ".lifecycle-resync.lock", **kwargs)

    def acquire(self) -> LifecycleLock:
        if self._descriptor is not None:
            raise RuntimeError("LifecycleLock instance is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        token = f"{os.getpid()} {uuid.uuid4().hex}"
        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if self._lock_is_abandoned():
                    raise RuntimeError(
                        "Abandoned lifecycle lock detected; refusing unsafe automatic "
                        f"recovery: {self.path}"
                    ) from None
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for lifecycle lock: {self.path}"
                    ) from None
                time.sleep(self.poll_interval_seconds)
                continue
            try:
                os.write(descriptor, f"{token}\n".encode("ascii"))
                os.fsync(descriptor)
            except Exception:
                os.close(descriptor)
                self.path.unlink(missing_ok=True)
                raise
            self._descriptor = descriptor
            self._token = token
            return self

    def lease(self) -> LifecycleLock:
        """Return an independent lease suitable for concurrent singleton service calls."""

        return type(self)(
            self.path,
            timeout_seconds=self.timeout_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
        )

    def release(self) -> None:
        descriptor = self._descriptor
        token = self._token
        if descriptor is None or token is None:
            return
        self._descriptor = None
        self._token = None
        os.close(descriptor)
        try:
            owner = self.path.read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            return
        if owner == token:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> LifecycleLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.release()

    def _lock_is_abandoned(self) -> bool:
        try:
            owner = self.path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return False
        except (OSError, UnicodeError):
            return False
        try:
            pid = int(owner.split(maxsplit=1)[0])
        except (ValueError, IndexError):
            # The owner may still be between O_EXCL creation and token fsync.
            return False
        return not self._process_is_alive(pid)

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process = kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                # Access denied means a protected process exists; other errors mean no owner.
                return ctypes.get_last_error() == 5
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(process)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            # Windows can reject signal 0 for a live process; age-based expiry remains.
            return True
        return True
