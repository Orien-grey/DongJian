"""Streaming SHA-256 fingerprinting with a stability check."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path
from typing import Callable

from .types import FileStat, FingerprintResult


DEFAULT_CHUNK_SIZE = 1024 * 1024
StatFunction = Callable[[Path], FileStat]


def stat_file(path: Path) -> FileStat:
    """Read the highest useful portable precision available from ``stat``."""

    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"not a regular file: {path}")
    mtime_ns = getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))
    return FileStat(size_bytes=int(info.st_size), mtime_ns=int(mtime_ns))


def _safe_stat(path: Path, stat_func: StatFunction) -> FileStat | None:
    try:
        return stat_func(path)
    except OSError:
        return None


def hash_file(path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE, stat_func: StatFunction = stat_file) -> FingerprintResult:
    """Hash in bounded chunks and reject a file whose metadata changes mid-read."""

    started = time.perf_counter_ns()
    try:
        before = stat_func(path)
    except OSError as exc:
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        return FingerprintResult(None, None, None, False, elapsed, "stat_error", str(exc))

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
    except OSError as exc:
        after = _safe_stat(path, stat_func)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        if after is None or after != before:
            return FingerprintResult(None, before, after, False, elapsed, "changed_during_scan", str(exc))
        return FingerprintResult(None, before, after, False, elapsed, "hash_error", str(exc))

    after = _safe_stat(path, stat_func)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if after is None or after != before:
        return FingerprintResult(None, before, after, False, elapsed, "changed_during_scan", "size or mtime changed while hashing")
    return FingerprintResult(digest.hexdigest(), before, after, True, elapsed)

