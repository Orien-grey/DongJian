"""Small project-local locking primitives.

The product only needs two kinds of ownership today: a process lifetime lock
for one project server and a short-lived schema lock for registry creation or
migration.  Keeping these primitives here avoids making the Registry depend
on the HTTP lifecycle module and keeps the implementation stdlib-only.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
from collections.abc import Iterator
from typing import Any


class LockUnavailable(RuntimeError):
    """Raised when a required project lock cannot be acquired."""


class FileLock:
    """An OS-released, one-byte advisory lock held by an open file handle."""

    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self._handle = None
        self._held = False

    def acquire(self, *, timeout: float = 0.0, poll_interval: float = 0.05) -> "FileLock":
        if self._held:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b" ")
                handle.flush()
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - production is Windows x64
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._handle = handle
                    self._held = True
                    return self
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise LockUnavailable(f"lock is already held: {self.path}") from exc
                    time.sleep(poll_interval)
        except Exception:
            handle.close()
            raise

    def write_metadata(self, value: dict[str, Any]) -> None:
        if not self._held or self._handle is None:
            raise RuntimeError("lock is not held")
        encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self._handle.seek(0)
        self._handle.truncate(0)
        self._handle.write(encoded)
        self._handle.flush()

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if self._held:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - production is Windows x64
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._held = False
            handle.close()

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()


_write_lock_guard = threading.Lock()
_write_locks: dict[str, threading.RLock] = {}
_connection_open_locks: dict[str, threading.Lock] = {}


def _thread_lock(path: Path | str) -> threading.RLock:
    key = str(Path(path).resolve()).casefold()
    with _write_lock_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _write_locks[key] = lock
        return lock


def _connection_open_lock(path: Path | str) -> threading.Lock:
    key = str(Path(path).resolve()).casefold()
    with _write_lock_guard:
        lock = _connection_open_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _connection_open_locks[key] = lock
        return lock


@contextmanager
def registry_write_mutex(path: Path | str) -> Iterator[None]:
    """Serialize Registry mutations within one server process.

    Cross-process ownership is provided by the project server lock.  This
    mutex is intentionally small and process-local so request writers cannot
    interleave with the active processing coordinator.
    """

    lock = _thread_lock(path)
    with lock:
        yield


@contextmanager
def registry_connection_open_mutex(path: Path | str) -> Iterator[None]:
    """Serialize DuckDB connection creation for one registry file.

    DuckDB supports concurrent operational connections, but Windows can reject
    simultaneous file-open attempts while another connection is establishing
    its file handle. This short critical section does not serialize queries or
    hold a lock for the lifetime of a connection.
    """

    lock = _connection_open_lock(path)
    with lock:
        yield
