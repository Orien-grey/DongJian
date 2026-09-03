"""Incremental, read-only discovery and fingerprint orchestration."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event
from typing import Any, Iterable
from uuid import uuid4

from . import paths
from .cancellation import CancellationRequested, check_cancel
from .detection import detect_file
from .discovery import discover, is_under_issue, issue_prefixes
from .fingerprint import hash_file, stat_file
from .logging import get_logger
from .registry import Registry, file_id_for, utc_now
from .types import (
    DetectionResult,
    DiscoveredFile,
    DiscoveryIssue,
    ExistingFile,
    FileOutcome,
    FingerprintResult,
    ScanSummary,
)


DEFAULT_WORKERS = max(1, min(8, os.cpu_count() or 1))
MAX_WORKERS = 32
LOGGER = get_logger("scan")


class ScanError(RuntimeError):
    """A scan could not be started or its registry could not be updated."""


def normalize_workers(workers: int | None) -> int:
    value = DEFAULT_WORKERS if workers is None else int(workers)
    if value < 1 or value > MAX_WORKERS:
        raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")
    return value


def _empty_detection() -> DetectionResult:
    return DetectionResult("unknown", "application/octet-stream", "not_detected", "low", "unknown")


def _reused_fingerprint(existing: ExistingFile, current: FileStat) -> FingerprintResult:
    return FingerprintResult(
        sha256=existing.sha256,
        before=current,
        after=current,
        stable=bool(existing.sha256),
        elapsed_ms=0.0,
        reused=True,
    )


def _classify(existing: ExistingFile | None, current: FileStat | None) -> str:
    if existing is None:
        return "new"
    if existing.current_presence_state != "present":
        return "changed"
    if current is None or existing.size_bytes != current.size_bytes or existing.mtime_ns != current.mtime_ns:
        return "changed"
    return "unchanged"


def _process_one(item: DiscoveredFile, existing: ExistingFile | None, force_rehash: bool) -> FileOutcome:
    started_at = utc_now()
    detection_started = time.perf_counter_ns()
    detection = detect_file(item.path, item.observed_extension)
    detection_ms = (time.perf_counter_ns() - detection_started) / 1_000_000

    try:
        current = stat_file(item.path)
    except OSError as exc:
        fingerprint = FingerprintResult(None, None, None, False, 0.0, "stat_error", str(exc))
        finished_at = utc_now()
        return FileOutcome(
            file_id="",
            source_root="",
            relative_path=item.relative_path,
            filename=item.filename,
            observed_extension=item.observed_extension,
            status="failed",
            classification=_classify(existing, None),
            detection=detection,
            fingerprint=fingerprint,
            started_at=started_at,
            finished_at=finished_at,
            detection_ms=detection_ms,
            error_code="stat_error",
            error_message=str(exc),
        )

    metadata_match = (
        existing is not None
        and existing.sha256
        and existing.size_bytes == current.size_bytes
        and existing.mtime_ns == current.mtime_ns
        and existing.current_presence_state == "present"
    )
    if metadata_match and not force_rehash:
        fingerprint = _reused_fingerprint(existing, current)
        # Detection itself can race with a writer.  A final stat turns that
        # race into a real hash attempt instead of accepting stale metadata.
        try:
            after_detection = stat_file(item.path)
        except OSError as exc:
            fingerprint = FingerprintResult(None, current, None, False, 0.0, "changed_during_scan", str(exc))
        else:
            if after_detection != current:
                fingerprint = hash_file(item.path)
    else:
        fingerprint = hash_file(item.path)

    status = "complete"
    error_code = detection.error_code
    error_message = detection.error_message
    if not fingerprint.stable:
        status = "failed"
        error_code = fingerprint.error_code or error_code or "hash_error"
        error_message = fingerprint.error_message or error_message
    elif detection.error_code:
        # A corrupt ZIP is still safely fingerprinted and classified, but the
        # detector warning remains visible as an isolated file failure.
        status = "failed"

    finished_at = utc_now()
    return FileOutcome(
        file_id="",
        source_root="",
        relative_path=item.relative_path,
        filename=item.filename,
        observed_extension=item.observed_extension,
        status=status,
        classification=_classify(existing, fingerprint.before),
        detection=detection,
        fingerprint=fingerprint,
        started_at=started_at,
        finished_at=finished_at,
        detection_ms=detection_ms,
        error_code=error_code,
        error_message=error_message,
    )


def _outcome_with_identity(outcome: FileOutcome, source_root: str) -> FileOutcome:
    return FileOutcome(
        file_id=file_id_for(source_root, outcome.relative_path),
        source_root=source_root,
        relative_path=outcome.relative_path,
        filename=outcome.filename,
        observed_extension=outcome.observed_extension,
        status=outcome.status,
        classification=outcome.classification,
        detection=outcome.detection,
        fingerprint=outcome.fingerprint,
        started_at=outcome.started_at,
        finished_at=outcome.finished_at,
        detection_ms=outcome.detection_ms,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
    )


def _write_log(summary: ScanSummary, issues: Iterable[DiscoveryIssue], registry_errors: Iterable[dict[str, Any]]) -> str:
    paths.LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = paths.LOGS_ROOT / f"scan-{summary.run_id}.jsonl"
    with log_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"event": "scan_summary", **summary.as_dict()}, ensure_ascii=False, default=str) + "\n")
        for issue in issues:
            handle.write(json.dumps({"event": "discovery_issue", **issue.__dict__}, ensure_ascii=False, default=str) + "\n")
        for error in registry_errors:
            handle.write(json.dumps({"event": "error", **error}, ensure_ascii=False, default=str) + "\n")
    return str(log_path)


def _new_run_id() -> str:
    return uuid4().hex


def scan_source(
    source: Path | str,
    *,
    workers: int | None = None,
    rehash: bool = False,
    registry_path: Path | str | None = None,
    cancel_event: Event | None = None,
) -> ScanSummary:
    """Scan *source* and update the project-local DuckDB registry.

    Only the registry and log under ``workspace/`` are written.  The source is
    opened for metadata, header inspection, and streaming reads only.
    """

    worker_count = normalize_workers(workers)
    check_cancel(cancel_event)
    try:
        source_root, discovered, issues = discover(source)
    except (OSError, ValueError) as exc:
        raise ScanError(str(exc)) from exc
    check_cancel(cancel_event)

    started_at = utc_now()
    perf_started = time.perf_counter_ns()
    run_id = _new_run_id()
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    if not registry_file.is_file():
        Registry.ensure_initialized(registry_file)
    registry = Registry.open(registry_file, initialize=False)
    registry_errors: list[dict[str, Any]] = []
    log_path: str | None = None
    try:
        LOGGER.debug("starting scan run_id=%s source_root=%s workers=%d rehash=%s", run_id, source_root, worker_count, rehash)
        registry.recover_incomplete_runs(source_root)
        registry.create_run(run_id, source_root, started_at, None)
        for issue in issues:
            if issue.is_error:
                registry.record_run_error(run_id, issue.path, issue.code, issue.message)

        previous = registry.load_files(source_root)
        summary = ScanSummary(run_id=run_id, source_root=source_root, started_at=started_at, finished_at=started_at, status="running")
        summary.discovered_count = len(discovered)
        summary.discovery_error_count = sum(1 for issue in issues if issue.is_error)
        summary.total_bytes = 0

        # Submit at most a small multiple of the worker count.  Completed
        # outcomes are written by this coordinator thread only.
        pending: dict[Future[FileOutcome], DiscoveredFile] = {}
        iterator = iter(discovered)
        hashing_ms = 0.0
        detection_ms = 0.0
        registry_write_ms = 0.0
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="chongzu-scan")
        try:
            exhausted = False
            while pending or not exhausted:
                check_cancel(cancel_event)
                while not exhausted and len(pending) < worker_count * 2:
                    check_cancel(cancel_event)
                    try:
                        item = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    pending[executor.submit(_process_one, item, previous.get(item.relative_path), rehash)] = item
                if not pending:
                    continue
                done, _ = wait(tuple(pending), timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    check_cancel(cancel_event)
                    item = pending.pop(future)
                    try:
                        raw_outcome = future.result()
                    except Exception as exc:  # noqa: BLE001 - isolate one file
                        now = utc_now()
                        raw_outcome = FileOutcome(
                            file_id="",
                            source_root="",
                            relative_path=item.relative_path,
                            filename=item.filename,
                            observed_extension=item.observed_extension,
                            status="failed",
                            classification=_classify(previous.get(item.relative_path), None),
                            detection=_empty_detection(),
                            fingerprint=FingerprintResult(None, None, None, False, 0.0, "worker_error", str(exc)),
                            started_at=now,
                            finished_at=now,
                            detection_ms=0.0,
                            error_code="worker_error",
                            error_message=str(exc),
                        )
                    outcome = _outcome_with_identity(raw_outcome, source_root)
                    summary.total_bytes += outcome.fingerprint.before.size_bytes if outcome.fingerprint.before else 0
                    if outcome.fingerprint.reused:
                        summary.reused_hash_count += 1
                    elif outcome.fingerprint.stable or outcome.fingerprint.error_code not in (None, ""):
                        summary.hashed_count += 1
                    if outcome.status != "complete":
                        summary.failed_count += 1
                    if outcome.classification == "new":
                        summary.new_count += 1
                    elif outcome.classification == "changed":
                        summary.changed_count += 1
                    else:
                        summary.unchanged_count += 1
                    hashing_ms += outcome.fingerprint.elapsed_ms
                    detection_ms += outcome.detection_ms
                    write_started = time.perf_counter_ns()
                    try:
                        registry.record_file_outcome(run_id, outcome)
                    except Exception as exc:  # DB errors are run-level, but keep context
                        registry_errors.append({"path": item.relative_path, "error_code": "registry_write_error", "error_message": str(exc)})
                        raise
                    registry_write_ms += (time.perf_counter_ns() - write_started) / 1_000_000
        except BaseException:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

        issue_prefix = issue_prefixes(issues)
        discovered_paths = {current.relative_path for current in discovered}
        missing_candidates = [
            item
            for relative, item in previous.items()
            if relative not in discovered_paths
            and item.current_presence_state != "missing"
            and not is_under_issue(relative, source_root, issue_prefix)
        ]
        missing_started = time.perf_counter_ns()
        summary.missing_count = registry.mark_missing(run_id, source_root, missing_candidates)
        registry_write_ms += (time.perf_counter_ns() - missing_started) / 1_000_000
        summary.exact_duplicate_paths = registry.exact_duplicate_paths(source_root)
        summary.hashing_ms = hashing_ms
        summary.detection_ms = detection_ms
        summary.registry_write_ms = registry_write_ms
        summary.finished_at = utc_now()
        summary.elapsed_ms = (time.perf_counter_ns() - perf_started) / 1_000_000
        summary.status = "complete" if not registry_errors else "failed"
        log_path = _write_log(summary, issues, registry_errors)
        registry.finish_run(summary, log_path)
        LOGGER.info(
            "scan complete run_id=%s source_root=%s discovered=%d failed=%d elapsed_ms=%.2f",
            summary.run_id,
            summary.source_root,
            summary.discovered_count,
            summary.failed_count,
            summary.elapsed_ms,
        )
        return summary
    except CancellationRequested:
        # A user stop is a normal interrupted run, not a processing failure.
        # Preserve all durable file observations already written before the
        # cancellation boundary and finalize the scan state for recovery.
        try:
            interrupted = ScanSummary(
                run_id=run_id,
                source_root=source_root,
                started_at=started_at,
                finished_at=utc_now(),
                status="interrupted",
                discovered_count=len(discovered),
                discovery_error_count=sum(1 for issue in issues if issue.is_error),
                elapsed_ms=(time.perf_counter_ns() - perf_started) / 1_000_000,
            )
            log_path = _write_log(
                interrupted,
                issues,
                [{"error_code": "interrupted", "error_message": "processing was cancelled by the user"}],
            )
            registry.finish_run(interrupted, log_path)
        except Exception:
            pass
        raise
    except Exception as exc:
        # Try to leave a durable failed run even if one file/DB operation was
        # unexpectedly broken.  The original exception is then surfaced to CLI.
        try:
            failed = ScanSummary(
                run_id=run_id,
                source_root=source_root,
                started_at=started_at,
                finished_at=utc_now(),
                status="failed",
                discovered_count=len(discovered),
                discovery_error_count=sum(1 for issue in issues if issue.is_error),
                elapsed_ms=(time.perf_counter_ns() - perf_started) / 1_000_000,
            )
            log_path = _write_log(failed, issues, [{"error_code": "run_error", "error_message": str(exc)}])
            registry.finish_run(failed, log_path)
        except Exception:
            pass
        raise ScanError(str(exc)) from exc
    finally:
        registry.close()


def benchmark_metrics(summary: ScanSummary) -> dict[str, float | int]:
    """Derive stable, human-readable throughput metrics from a scan summary."""

    seconds = summary.elapsed_ms / 1000 if summary.elapsed_ms > 0 else 0.0
    total_mb = summary.total_bytes / (1024 * 1024)
    return {
        "file_count": summary.discovered_count,
        "total_bytes": summary.total_bytes,
        "files_per_sec": summary.discovered_count / seconds if seconds else 0.0,
        "mb_per_sec": total_mb / seconds if seconds else 0.0,
        "hashing_time_ms": summary.hashing_ms,
        "detection_time_ms": summary.detection_ms,
        "registry_write_time_ms": summary.registry_write_ms,
        "wall_clock_time_ms": summary.elapsed_ms,
        "hashed": summary.hashed_count,
        "reused": summary.reused_hash_count,
    }
