"""Single-writer orchestration for deterministic cleaning and profiling."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
from pathlib import Path
from queue import Full, Queue
from threading import Event, Lock, Thread
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from dongjian import paths
from dongjian.cancellation import check_cancel
from dongjian.extract.unified import UnifiedExtractionSummary, extract_unified
from dongjian.fingerprint import hash_file
from dongjian.locking import registry_write_mutex
from dongjian.processing_policy import RegistryFileInfo, plan_processing
from dongjian.registry import Registry, canonical_source_root, utc_now
from dongjian.types import FileOutcome, ScanSummary

from .models import CleaningAssetResult, CleaningSummary
from .quality import assess_table_quality, assess_text_quality, cleaning_failure_issue
from .table_cleaner import CLEANER_NAME as TABLE_CLEANER, clean_table
from .text_cleaner import CLEANER_NAME as TEXT_CLEANER, clean_text
from ..extract.artifacts import artifact_absolute


MAX_CLEANING_WORKERS = 4
MAX_READY_PROCESS_QUEUE = 8
ProgressCallback = Callable[..., None]


class CleaningError(RuntimeError):
    """Raised when the cleaning coordinator cannot prepare its source."""


class RegisteredFileReprocessError(CleaningError):
    """Raised when one registered source cannot be safely reprocessed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_cleaning_workers(workers: int | None) -> int:
    value = 2 if workers is None else int(workers)
    if value < 1 or value > MAX_CLEANING_WORKERS:
        raise ValueError(f"cleaning workers must be between 1 and {MAX_CLEANING_WORKERS}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _raw_identity(candidate: Mapping[str, Any], workspace_root: Path) -> str:
    raw = candidate.get("raw_artifact_path")
    if not raw:
        return "missing-raw-artifact"
    return _sha256_file(artifact_absolute(str(raw), workspace_root))


def cleaning_identity(
    candidate: Mapping[str, Any],
    raw_artifact_identity: str,
    *,
    drop_exact_duplicates: bool = False,
) -> str:
    asset_type = str(candidate.get("asset_type") or "")
    cleaner = TABLE_CLEANER if asset_type == "table" else TEXT_CLEANER
    cleaner_version = paths.TABLE_CLEANER_VERSION if asset_type == "table" else paths.TEXT_CLEANER_VERSION
    payload = {
        "asset_id": str(candidate.get("asset_id") or ""),
        "asset_type": asset_type,
        "content_sha256": str(candidate.get("content_sha256") or ""),
        "raw_artifact_identity": raw_artifact_identity,
        "cleaner": cleaner,
        "cleaner_version": cleaner_version,
        "config_version": paths.CLEANING_CONFIG_VERSION,
        "profile_version": paths.PROFILE_CONFIG_VERSION,
        "drop_exact_duplicates": bool(drop_exact_duplicates),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _base_result(
    candidate: Mapping[str, Any],
    *,
    raw_identity: str,
    identity: str,
    run_id: str,
) -> CleaningAssetResult:
    asset_type = str(candidate.get("asset_type") or "")
    return CleaningAssetResult(
        asset_id=str(candidate["asset_id"]),
        asset_type=asset_type,
        file_id=str(candidate["file_id"]),
        content_sha256=str(candidate["content_sha256"]),
        source_root=str(candidate["source_root"]),
        source_relative_path=str(candidate.get("source_relative_path") or ""),
        raw_artifact_identity=raw_identity,
        cleaner=TABLE_CLEANER if asset_type == "table" else TEXT_CLEANER,
        cleaner_version=paths.TABLE_CLEANER_VERSION if asset_type == "table" else paths.TEXT_CLEANER_VERSION,
        config_version=paths.CLEANING_CONFIG_VERSION,
        cleaning_run_id=run_id,
        cleaning_identity=identity,
    )


def _clean_one(
    candidate: Mapping[str, Any],
    *,
    workspace_root: Path,
    raw_identity: str,
    identity: str,
    run_id: str,
    drop_exact_duplicates: bool,
) -> CleaningAssetResult:
    if str(candidate.get("asset_type")) == "table":
        result = clean_table(
            candidate,
            workspace_root=workspace_root,
            raw_artifact_identity=raw_identity,
            cleaning_identity=identity,
            cleaning_run_id=run_id,
            drop_exact_duplicates=drop_exact_duplicates,
        )
    else:
        result = clean_text(
            candidate,
            workspace_root=workspace_root,
            raw_artifact_identity=raw_identity,
            cleaning_identity=identity,
            cleaning_run_id=run_id,
        )
    if result.status == "successful":
        if result.asset_type == "table":
            result.quality_status, result.issues = assess_table_quality(
                candidate, result.profile, cleaning_identity=identity
            )
        else:
            result.quality_status, result.issues = assess_text_quality(
                candidate, result.profile, cleaning_identity=identity
            )
    else:
        result.quality_status = "needs_review"
        result.issues = [
            cleaning_failure_issue(
                candidate,
                cleaning_identity=identity,
                error_category=result.error_category,
                error_message=result.error_message,
            )
        ]
    return result


def _artifacts_exist(reusable: Mapping[str, Any], workspace_root: Path) -> bool:
    paths_to_check = [
        reusable.get("manifest_artifact_path"),
        reusable.get("profile_artifact_path"),
    ]
    normalized = reusable.get("normalized_artifact_path")
    if normalized:
        paths_to_check.append(normalized)
    try:
        return bool(paths_to_check) and all(artifact_absolute(str(item), workspace_root).is_file() for item in paths_to_check if item)
    except (OSError, ValueError):
        return False


def _summary_catalog(registry: Registry, source_root: str) -> tuple[dict[str, int], int, int]:
    catalog = registry.catalog_summary(source_root)
    row = registry.connection.execute(
        "SELECT COALESCE(SUM(\"rows\"), 0), COALESCE(SUM(\"chars\"), 0) FROM catalog_assets WHERE source_root=?",
        [source_root],
    ).fetchone()
    return catalog, int(row[0] or 0), int(row[1] or 0)


def clean_source(
    source: Path | str,
    *,
    workers: int | None = None,
    force: bool = False,
    drop_exact_duplicates: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    registry: Registry | None = None,
    extraction_summary: UnifiedExtractionSummary | None = None,
    file_ids: set[str] | None = None,
    _skip_catalog_summary: bool = False,
    _skip_recovery: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> CleaningSummary:
    """Clean and profile current assets for one source without re-extracting."""

    wall_started = time.perf_counter_ns()
    worker_count = normalize_cleaning_workers(workers)
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    source_root = canonical_source_root(source, require_directory=True)
    summary = CleaningSummary(source_root=source_root)
    if extraction_summary is not None:
        summary.files_discovered = extraction_summary.files_discovered
        summary.files_supported = extraction_summary.supported
        summary.files_unsupported = extraction_summary.unsupported
        summary.extracted = extraction_summary.processed
        summary.reused_extraction = extraction_summary.reused
        summary.extraction_failures = extraction_summary.failed

    owns_registry = registry is None
    registry = registry or Registry.open(registry_file, initialize=False)
    try:
        check_cancel(cancel_event)
        if not _skip_recovery:
            registry.recover_incomplete_cleaning(source_root)
        if extraction_summary is None:
            summary.files_discovered = registry.count_present_files(source_root)
            summary.files_supported = int(
                registry.connection.execute(
                    "SELECT COUNT(*) FROM files WHERE source_root=? AND current_presence_state='present' AND support_status='supported'",
                    [source_root],
                ).fetchone()[0]
                or 0
            )
            summary.files_unsupported = summary.files_discovered - summary.files_supported
        candidates = registry.cleaning_candidates(source_root)
        if file_ids is not None:
            candidates = [item for item in candidates if str(item.get("file_id") or "") in file_ids]
        summary.table_assets = sum(1 for item in candidates if item.get("asset_type") == "table")
        summary.text_assets = sum(1 for item in candidates if item.get("asset_type") == "text")
        completed = 0

        def emit(asset_name: str | None, substage: str, stage: str = "clean") -> None:
            if progress_callback is None:
                return
            progress = 0.45 + (0.30 * completed / max(1, len(candidates)))
            try:
                progress_callback(
                    stage,
                    progress,
                    current_file=asset_name,
                    completed=completed,
                    total=len(candidates),
                    current_substage=substage,
                )
            except TypeError:
                progress_callback(stage, progress)
        pending: dict[Future[CleaningAssetResult], tuple[CleaningAssetResult, Any]] = {}
        candidate_iter = iter(candidates)
        exhausted = False
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="dongjian-clean")
        try:
            while pending or not exhausted:
                check_cancel(cancel_event)
                while not exhausted and len(pending) < worker_count * 2:
                    check_cancel(cancel_event)
                    try:
                        candidate = next(candidate_iter)
                    except StopIteration:
                        exhausted = True
                        break
                    emit(str(candidate.get("source_relative_path") or ""), "准备确定性清洗")
                    try:
                        raw_identity = _raw_identity(candidate, workspace)
                    except Exception as exc:
                        raw_identity = f"missing:{candidate.get('raw_artifact_path') or 'none'}"
                        identity = cleaning_identity(candidate, raw_identity, drop_exact_duplicates=drop_exact_duplicates)
                        result = _base_result(candidate, raw_identity=raw_identity, identity=identity, run_id=f"crun_{uuid4().hex}")
                        result.status = "failed"
                        result.error_category = "raw_artifact_error"
                        result.error_message = str(exc)
                        result.issues = [
                            cleaning_failure_issue(
                                candidate,
                                cleaning_identity=identity,
                                error_category=result.error_category,
                                error_message=result.error_message,
                            )
                        ]
                        result.quality_status = "needs_review"
                        started_at = utc_now()
                        registry.start_cleaning_run(result, started_at=started_at, force=force)
                        started = time.perf_counter_ns()
                        registry.record_cleaning_result(result, started_at=started_at, finished_at=utc_now(), force=force)
                        summary.cleaning_failures += 1
                        summary.duckdb_write_ms += (time.perf_counter_ns() - started) / 1_000_000
                        completed += 1
                        emit(str(candidate.get("source_relative_path") or ""), "清洗失败")
                        continue
                    identity = cleaning_identity(candidate, raw_identity, drop_exact_duplicates=drop_exact_duplicates)
                    reusable = None if force else registry.reusable_cleaning(identity)
                    if reusable is not None and _artifacts_exist(reusable, workspace):
                        summary.reused_cleaning += 1
                        completed += 1
                        emit(str(candidate.get("source_relative_path") or ""), "复用已有清洗结果")
                        continue
                    result = _base_result(candidate, raw_identity=raw_identity, identity=identity, run_id=f"crun_{uuid4().hex}")
                    started_at = utc_now()
                    registry.start_cleaning_run(result, started_at=started_at, force=force)
                    future = executor.submit(
                        _clean_one,
                        candidate,
                        workspace_root=workspace,
                        raw_identity=raw_identity,
                        identity=identity,
                        run_id=result.cleaning_run_id,
                        drop_exact_duplicates=drop_exact_duplicates,
                    )
                    pending[future] = (result, started_at)
                if not pending:
                    continue
                done, _ = wait(tuple(pending), timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    check_cancel(cancel_event)
                    prepared, started_at = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # isolate one asset worker
                        result = prepared
                        result.status = "failed"
                        result.error_category = "cleaning_worker_error"
                        result.error_message = str(exc)
                        result.quality_status = "needs_review"
                        result.issues = [
                            cleaning_failure_issue(
                                prepared.__dict__,
                                cleaning_identity=prepared.cleaning_identity,
                                error_category=result.error_category,
                                error_message=result.error_message,
                            )
                        ]
                    registry_started = time.perf_counter_ns()
                    registry.record_cleaning_result(
                        result,
                        started_at=started_at,
                        finished_at=utc_now(),
                        force=force,
                    )
                    summary.duckdb_write_ms += (time.perf_counter_ns() - registry_started) / 1_000_000
                    summary.normalize_ms += result.timings.normalize_ms
                    summary.profile_ms += result.timings.profile_ms
                    summary.parquet_write_ms += result.timings.parquet_write_ms
                    summary.artifact_write_ms += result.timings.artifact_write_ms
                    if result.status == "failed":
                        summary.cleaning_failures += 1
                    else:
                        summary.cleaned += 1
                    completed += 1
                    emit(str(prepared.source_relative_path or ""), "确定性清洗完成")
        except BaseException:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
    finally:
        if owns_registry:
            registry.close()
    if progress_callback is not None:
        progress_callback("profile", 0.78)
    summary.cleaning_wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    summary.wall_time_ms = summary.cleaning_wall_time_ms
    if _skip_catalog_summary:
        if progress_callback is not None:
            progress_callback("catalog", 0.94)
        return summary
    if owns_registry:
        registry = Registry.open(registry_file, initialize=False)
        try:
            catalog, rows, chars = _summary_catalog(registry, source_root)
        finally:
            registry.close()
    else:
        catalog, rows, chars = _summary_catalog(registry, source_root)
    if progress_callback is not None:
        progress_callback("catalog", 0.94)
    summary.rows = rows
    summary.chars = chars
    summary.table_assets = int(catalog.get("table_assets", summary.table_assets))
    summary.text_assets = int(catalog.get("text_assets", summary.text_assets))
    summary.ready = int(catalog.get("ready", 0))
    summary.needs_review = int(catalog.get("needs_review", 0))
    summary.unusable = int(catalog.get("unusable", 0))
    summary.quality_issues = int(catalog.get("quality_issues", 0))
    summary.semantic_pending = int(catalog.get("semantic_pending", 0))
    return summary


def _process_file_unit(
    source: Path | str,
    *,
    row: Mapping[str, Any],
    index: int,
    total: int | None,
    workers: int,
    force: bool,
    drop_exact_duplicates: bool,
    registry_path: Path,
    workspace_root: Path,
    scan_summary: Any,
    registry: Registry,
    emit: Callable[..., None],
    cancel_event: Event | None,
    vision_mode: str,
    vision_provider: Any | None,
) -> tuple[UnifiedExtractionSummary, CleaningSummary] | None:
    """Process one registered file and publish its local completion boundary."""

    check_cancel(cancel_event)
    file_id = str(row.get("file_id") or "")
    relative_path = str(row.get("relative_path") or "")
    denominator = max(1, int(total) if total is not None else index + 2)

    def scaled(value: float) -> float:
        bounded = max(0.0, min(1.0, float(value)))
        if total is None:
            # An unknown total is intentionally never rendered as complete;
            # the task switches to a determinate fraction after scan end.
            return min(0.99, (index + bounded) / denominator)
        return (index + bounded) / denominator

    if row.get("support_status") != "supported":
        emit(
            "skipped",
            scaled(1.0),
            current_file=relative_path,
            current_file_id=file_id,
            completed=index + 1,
            total=total,
            current_substage="unsupported format",
        )
        return None

    def scoped_callback(stage: str, value: float, **metadata: Any) -> None:
        emit(
            stage,
            scaled(value),
            current_file=relative_path,
            current_file_id=file_id,
            completed=index,
            total=total,
            current_page=metadata.get("current_page"),
            current_page_total=metadata.get("total_pages"),
            current_substage=metadata.get("current_substage"),
            run_id=metadata.get("run_id"),
        )

    with registry_write_mutex(registry_path):
        extraction = extract_unified(
            source,
            workers=min(workers, 2),
            force=force,
            registry_path=registry_path,
            workspace_root=workspace_root,
            _scan_summary=scan_summary,
            file_ids={file_id},
            _skip_catalog_summary=True,
            _skip_recovery=True,
            cancel_event=cancel_event,
            progress_callback=scoped_callback,
            vision_mode=vision_mode,
            vision_provider=vision_provider,
            registry=registry,
        )
        emit(
            "clean",
            scaled(0.52),
            current_file=relative_path,
            current_file_id=file_id,
            completed=index,
            total=total,
            current_substage="准备确定性清洗",
            run_id=extraction.run_id,
        )
        if vision_mode == "ai_vision":
            emit(
                "clean",
                scaled(0.52),
                current_file=relative_path,
                current_file_id=file_id,
                completed=index,
                total=total,
                current_substage="cleaning_assets",
                run_id=extraction.run_id,
            )
        cleaned = clean_source(
            source,
            workers=workers,
            force=force,
            drop_exact_duplicates=drop_exact_duplicates,
            registry_path=registry_path,
            workspace_root=workspace_root,
            extraction_summary=extraction,
            file_ids={file_id},
            _skip_catalog_summary=True,
            _skip_recovery=True,
            progress_callback=scoped_callback,
            cancel_event=cancel_event,
            registry=registry,
        )

    emit(
        "local_ready",
        scaled(1.0),
        current_file=relative_path,
        current_file_id=file_id,
        completed=index + 1,
        total=total,
        current_substage="Catalog / Search ready",
        run_id=extraction.run_id,
    )
    return extraction, cleaned


def reprocess_registered_file(
    source_root: Path | str,
    *,
    file_id: str,
    expected_sha256: str,
    registry_path: Path,
    workspace_root: Path,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> CleaningSummary:
    """Reprocess one already registered file without scanning its directory."""

    source = canonical_source_root(source_root, require_directory=True)
    registry = Registry.open(registry_path, initialize=False)
    try:
        cursor = registry.connection.execute(
            "SELECT * FROM files WHERE file_id=? AND source_root=? AND current_presence_state='present' LIMIT 1",
            [file_id, source],
        )
        found = cursor.fetchone()
        if found is None:
            raise RegisteredFileReprocessError("FILE_NOT_FOUND", "registered file was not found")
        row = dict(zip([item[0] for item in cursor.description], found))
        relative_path = str(row.get("relative_path") or "")
        source_path = (Path(source) / Path(relative_path)).resolve()
        try:
            source_path.relative_to(Path(source))
        except ValueError as exc:
            raise RegisteredFileReprocessError("SOURCE_PATH_INVALID", "registered source path is outside its source root") from exc
        if not source_path.is_file():
            raise RegisteredFileReprocessError("SOURCE_NOT_FOUND", "registered source file is unavailable")
        if str(row.get("sha256") or "") != str(expected_sha256 or ""):
            raise RegisteredFileReprocessError("STALE_REGISTRY", "registered source identity changed before reprocessing")
        fingerprint = hash_file(source_path)
        if not fingerprint.stable or not fingerprint.sha256:
            raise RegisteredFileReprocessError("STALE_SOURCE", "source file changed or could not be fingerprinted")
        if fingerprint.sha256 != str(expected_sha256):
            raise RegisteredFileReprocessError("STALE_SOURCE", "source file changed since it was registered")
        started_at = utc_now()
        scan_summary = ScanSummary(
            run_id=f"reprocess_{uuid4().hex}",
            source_root=source,
            started_at=started_at,
            finished_at=started_at,
            status="completed",
            discovered_count=1,
            hashed_count=1,
            unchanged_count=1,
            total_bytes=int(fingerprint.after.size_bytes if fingerprint.after is not None else source_path.stat().st_size),
            elapsed_ms=float(fingerprint.elapsed_ms),
            hashing_ms=float(fingerprint.elapsed_ms),
        )

        def emit(stage: str, value: float, **metadata: Any) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(
                    stage,
                    value,
                    current_file=relative_path,
                    current_file_id=file_id,
                    completed=metadata.pop("completed", None),
                    total=1,
                    current_page=metadata.pop("current_page", None),
                    current_page_total=metadata.pop("current_page_total", None),
                    current_substage=metadata.pop("current_substage", None),
                    run_id=metadata.pop("run_id", None),
                )
            except TypeError:
                progress_callback(stage, value)

        result = _process_file_unit(
            source,
            row=row,
            index=0,
            total=1,
            workers=1,
            force=True,
            drop_exact_duplicates=False,
            registry_path=registry_path,
            workspace_root=workspace_root,
            scan_summary=scan_summary,
            registry=registry,
            emit=emit,
            cancel_event=cancel_event,
            vision_mode="local",
            vision_provider=None,
        )
        summary = CleaningSummary(source_root=source, files_discovered=1, files_supported=1)
        if result is not None:
            extraction, cleaned = result
            summary.extracted = int(extraction.processed)
            summary.extraction_failures = int(extraction.failed)
            summary.table_assets = int(extraction.table_assets)
            summary.text_assets = int(extraction.text_assets)
            summary.cleaned = int(cleaned.cleaned)
            summary.reused_cleaning = int(cleaned.reused_cleaning)
            summary.cleaning_failures = int(cleaned.cleaning_failures)
            summary.quality_issues = int(cleaned.quality_issues)
            summary.normalize_ms = float(cleaned.normalize_ms)
            summary.profile_ms = float(cleaned.profile_ms)
            summary.parquet_write_ms = float(cleaned.parquet_write_ms)
            summary.artifact_write_ms = float(cleaned.artifact_write_ms)
            summary.duckdb_write_ms = float(cleaned.duckdb_write_ms)
        summary.wall_time_ms = float(scan_summary.elapsed_ms)
        return summary
    finally:
        registry.close()


class _StreamingConsumerError(RuntimeError):
    """The bounded processing consumer stopped before draining its queue."""


def _process_source_streaming(
    source: Path | str,
    *,
    workers: int,
    force: bool,
    drop_exact_duplicates: bool,
    registry_path: Path,
    workspace_root: Path,
    progress_callback: ProgressCallback | None,
    cancel_event: Event | None,
    vision_mode: str,
    vision_provider: Any | None,
    _wall_started: int | None = None,
    _scan_batch_size: int | None = None,
) -> CleaningSummary:
    """Overlap lazy scan batches with one bounded extraction/clean consumer."""

    from dongjian.scan import scan_source

    wall_started = _wall_started or time.perf_counter_ns()
    source_root = canonical_source_root(source, require_directory=True)
    ready_queue: Queue[object] = Queue(maxsize=max(2, min(MAX_READY_PROCESS_QUEUE, workers * 2)))
    sentinel = object()
    consumer_failed = Event()
    state_lock = Lock()
    state: dict[str, Any] = {
        "discovered_count": 0,
        "registered_count": 0,
        "ready_local_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "scan_complete": False,
        "total": None,
        "last_progress": 0.0,
        "registered_ids": set(),
        "ready_ids": set(),
        "failed_ids": set(),
        "skipped_ids": set(),
    }
    result: dict[str, Any] = {"aggregate": None, "error": None}
    scan_result: Any = None

    def state_snapshot() -> dict[str, Any]:
        with state_lock:
            return {
                "discovered_count": int(state["discovered_count"]),
                "registered_count": int(state["registered_count"]),
                "ready_local_count": int(state["ready_local_count"]),
                "failed_count": int(state["failed_count"]),
                "skipped_count": int(state["skipped_count"]),
                "scan_complete": bool(state["scan_complete"]),
                "total": state["total"],
            }

    def record_progress(stage: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        file_id = str(metadata.get("current_file_id") or "")
        with state_lock:
            if stage == "local_ready" and file_id and file_id not in state["ready_ids"]:
                state["ready_ids"].add(file_id)
                state["ready_local_count"] += 1
            elif stage == "failed" and file_id and file_id not in state["failed_ids"]:
                state["failed_ids"].add(file_id)
                state["failed_count"] += 1
            elif stage == "skipped" and file_id and file_id not in state["skipped_ids"]:
                state["skipped_ids"].add(file_id)
                state["skipped_count"] += 1
            values = {
                "discovered_count": int(state["discovered_count"]),
                "registered_count": int(state["registered_count"]),
                "ready_local_count": int(state["ready_local_count"]),
                "failed_count": int(state["failed_count"]),
                "skipped_count": int(state["skipped_count"]),
                "scan_complete": bool(state["scan_complete"]),
                "total": state["total"],
            }
        values.update(metadata)
        # Once the scan has settled, the final file count is authoritative.
        # Before then, a missing total is deliberate and must remain unknown.
        with state_lock:
            if state["scan_complete"]:
                values["total"] = state["total"]
        return values

    def emit(stage: str, value: float, **metadata: Any) -> None:
        values = record_progress(stage, metadata)
        with state_lock:
            value = max(float(value), float(state["last_progress"]))
            state["last_progress"] = min(1.0, value)
        values.update(
            {
                "discovered_count": values["discovered_count"],
                "registered_count": values["registered_count"],
                "ready_local_count": values["ready_local_count"],
                "failed_count": values["failed_count"],
                "skipped_count": values["skipped_count"],
                "scan_complete": values["scan_complete"],
            }
        )
        if progress_callback is None:
            return
        try:
            progress_callback(stage, value, **values)
        except TypeError:
            progress_callback(stage, value)

    def on_discovered(scan_summary: Any) -> None:
        with state_lock:
            state["discovered_count"] = max(state["discovered_count"], int(scan_summary.discovered_count))
            count = state["discovered_count"]
        # Discovery is intentionally sampled.  The first event gives the
        # latency boundary; subsequent events avoid turning a large scan into
        # a progress-callback storm.
        if count == 1 or count % 16 == 0:
            emit("discovering", 0.01, current_substage="discovering files")

    def enqueue(outcome: FileOutcome, scan_summary: Any) -> None:
        with state_lock:
            state["discovered_count"] = max(state["discovered_count"], int(scan_summary.discovered_count))
            state["registered_count"] += 1
            state["registered_ids"].add(outcome.file_id)
            registered_count = state["registered_count"]
        emit(
            "registering",
            0.02,
            current_file=outcome.relative_path,
            current_file_id=outcome.file_id,
            current_substage=f"registered {registered_count}",
        )
        while True:
            check_cancel(cancel_event)
            if consumer_failed.is_set():
                raise _StreamingConsumerError("streaming processing consumer stopped")
            try:
                ready_queue.put((outcome, scan_summary), timeout=0.1)
                return
            except Full:
                continue

    def consume() -> None:
        registry: Registry | None = None
        aggregate = CleaningSummary(source_root=source_root)
        processed_index = 0
        try:
            registry = Registry.open(registry_path, initialize=False)
            with registry_write_mutex(registry_path):
                registry.recover_incomplete_extractions(source_root)
                registry.recover_incomplete_cleaning(source_root)
            while True:
                item = ready_queue.get()
                if item is sentinel:
                    break
                outcome, scan_summary = item  # type: ignore[misc]
                processed_index += 1
                with state_lock:
                    total = state["total"] if state["scan_complete"] else None
                info = RegistryFileInfo(
                    file_id=outcome.file_id,
                    detected_type=outcome.detection.detected_type,
                    mime_like_type=outcome.detection.mime_like_type,
                    observed_extension=outcome.observed_extension,
                    routing_class=outcome.detection.routing_class,
                )
                plan = plan_processing(info)
                row = {
                    "file_id": outcome.file_id,
                    "relative_path": outcome.relative_path,
                    "support_status": plan.support_status.value,
                }
                try:
                    if outcome.fingerprint.sha256 is None:
                        emit(
                            "failed",
                            min(0.99, processed_index / max(1, processed_index + 1)),
                            current_file=outcome.relative_path,
                            current_file_id=outcome.file_id,
                            current_substage="hash failed; continuing scan",
                            total=total,
                        )
                        continue
                    result_pair = _process_file_unit(
                        source,
                        row=row,
                        index=processed_index - 1,
                        total=total,
                        workers=workers,
                        force=force,
                        drop_exact_duplicates=drop_exact_duplicates,
                        registry_path=registry_path,
                        workspace_root=workspace_root,
                        scan_summary=scan_summary,
                        registry=registry,
                        emit=emit,
                        cancel_event=cancel_event,
                        vision_mode=vision_mode,
                        vision_provider=vision_provider,
                    )
                except CancellationRequested:
                    raise
                except Exception as exc:  # one file cannot stop the stream
                    emit(
                        "failed",
                        min(0.99, processed_index / max(1, processed_index + 1)),
                        current_file=outcome.relative_path,
                        current_file_id=outcome.file_id,
                        current_substage=f"file failed; continuing ({type(exc).__name__})",
                        total=total,
                    )
                    continue
                if result_pair is None:
                    continue
                extraction, cleaned = result_pair
                aggregate.extracted += extraction.processed
                aggregate.reused_extraction += extraction.reused
                aggregate.extraction_failures += extraction.failed
                aggregate.cleaned += cleaned.cleaned
                aggregate.reused_cleaning += cleaned.reused_cleaning
                aggregate.cleaning_failures += cleaned.cleaning_failures
                aggregate.normalize_ms += cleaned.normalize_ms
                aggregate.profile_ms += cleaned.profile_ms
                aggregate.parquet_write_ms += cleaned.parquet_write_ms
                aggregate.artifact_write_ms += cleaned.artifact_write_ms
                aggregate.duckdb_write_ms += cleaned.duckdb_write_ms
            result["aggregate"] = aggregate
        except BaseException as exc:  # noqa: BLE001 - surfaced by coordinator
            result["error"] = exc
            consumer_failed.set()
            # Keep the producer from deadlocking on a full bounded queue while
            # it records the interrupted/failed scan run.
            while True:
                item = ready_queue.get()
                if item is sentinel:
                    break
        finally:
            if registry is not None:
                registry.close()

    consumer = Thread(target=consume, name="dongjian-stream-process", daemon=True)
    consumer.start()
    scan_error: BaseException | None = None
    try:
        scan_result = scan_source(
            source,
            workers=min(workers, 2),
            registry_path=registry_path,
            cancel_event=cancel_event,
            outcome_callback=enqueue,
            discovery_callback=on_discovered,
            registry_batch_size=_scan_batch_size,
        )
        with state_lock:
            state["discovered_count"] = max(state["discovered_count"], int(scan_result.discovered_count))
            state["total"] = int(scan_result.discovered_count)
            state["scan_complete"] = True
        emit(
            "scanning",
            0.20,
            total=int(scan_result.discovered_count),
            current_substage="scan complete; finishing queued files",
            scan_complete=True,
        )
    except BaseException as exc:  # noqa: BLE001 - preserve cancellation type
        scan_error = exc
        consumer_failed.set()
    finally:
        while consumer.is_alive():
            try:
                ready_queue.put(sentinel, timeout=0.1)
                break
            except Full:
                continue
        consumer.join()

    if scan_error is not None:
        raise scan_error
    if result["error"] is not None:
        raise result["error"]
    aggregate = result["aggregate"]
    if not isinstance(aggregate, CleaningSummary):
        raise _StreamingConsumerError("streaming processing produced no summary")

    registry = Registry.open(registry_path, initialize=False)
    try:
        rows = registry.list_files(source_root, state="present", limit=100_000)
        aggregate.files_discovered = int(scan_result.discovered_count) if scan_result is not None else len(rows)
        aggregate.files_supported = sum(1 for row in rows if row.get("support_status") == "supported")
        aggregate.files_unsupported = len(rows) - aggregate.files_supported
        catalog, rows_count, chars = _summary_catalog(registry, source_root)
    finally:
        registry.close()
    aggregate.table_assets = int(catalog.get("table_assets", 0))
    aggregate.text_assets = int(catalog.get("text_assets", 0))
    aggregate.ready = int(catalog.get("ready", 0))
    aggregate.needs_review = int(catalog.get("needs_review", 0))
    aggregate.unusable = int(catalog.get("unusable", 0))
    aggregate.quality_issues = int(catalog.get("quality_issues", 0))
    aggregate.semantic_pending = int(catalog.get("semantic_pending", 0))
    aggregate.rows = rows_count
    aggregate.chars = chars
    aggregate.scan_metrics = scan_result.as_dict() if scan_result is not None else {}
    if scan_result is not None:
        aggregate.scan_metrics.update(
            {
                "ready_queue_max": ready_queue.maxsize,
                "pending_futures_max": scan_result.candidate_queue_max,
            }
        )
    aggregate.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    aggregate.cleaning_wall_time_ms = aggregate.wall_time_ms
    snapshot = state_snapshot()
    emit(
        "completed",
        1.0,
        completed=snapshot["ready_local_count"],
        total=aggregate.files_discovered,
        scan_complete=True,
        current_substage="local processing complete",
    )
    return aggregate


def _process_source_incremental(
    source: Path | str,
    *,
    workers: int,
    force: bool,
    drop_exact_duplicates: bool,
    registry_path: Path,
    workspace_root: Path,
    progress_callback: ProgressCallback | None,
    cancel_event: Event | None,
    vision_mode: str,
    vision_provider: Any | None,
    _registry: Registry | None = None,
    _scan_summary=None,
    _wall_started: int | None = None,
    _scan_batch_size: int | None = None,
) -> CleaningSummary:
    """Complete a directory in file-sized extraction/cleaning units."""

    from dongjian.scan import scan_source

    wall_started = _wall_started or time.perf_counter_ns()
    check_cancel(cancel_event)
    if _scan_summary is None and _registry is None:
        return _process_source_streaming(
            source,
            workers=workers,
            force=force,
            drop_exact_duplicates=drop_exact_duplicates,
            registry_path=registry_path,
            workspace_root=workspace_root,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            vision_mode=vision_mode,
            vision_provider=vision_provider,
            _wall_started=wall_started,
            _scan_batch_size=_scan_batch_size,
        )
    if _scan_summary is None:
        # scan_source serializes only its short registry mutations. Keeping
        # the scan outside one directory-wide mutex leaves operational catalog
        # readers available between bounded observation batches.
        scan_summary = scan_source(
            source,
            workers=min(workers, 2),
            registry_path=registry_path,
            cancel_event=cancel_event,
            registry_batch_size=_scan_batch_size,
        )
    else:
        scan_summary = _scan_summary
    source_root = canonical_source_root(source, require_directory=True)
    if _registry is None:
        shared_registry = Registry.open(registry_path, initialize=False)
        try:
            return _process_source_incremental(
                source,
                workers=workers,
                force=force,
                drop_exact_duplicates=drop_exact_duplicates,
                registry_path=registry_path,
                workspace_root=workspace_root,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                vision_mode=vision_mode,
                vision_provider=vision_provider,
                _registry=shared_registry,
                _scan_summary=scan_summary,
                _wall_started=wall_started,
                _scan_batch_size=_scan_batch_size,
            )
        finally:
            shared_registry.close()
    registry = _registry
    # The scan already recovers incomplete scan runs. Recover the two
    # extraction/cleaning stages once for this coordinator instead of asking
    # every per-file route invocation to repeat the same directory UPDATE.
    with registry_write_mutex(registry_path):
        registry.recover_incomplete_extractions(source_root)
        registry.recover_incomplete_cleaning(source_root)
    rows = registry.list_files(source_root, state="present", limit=100_000)
    total = len(rows)
    aggregate = CleaningSummary(
        source_root=source_root,
        files_discovered=scan_summary.discovered_count,
        files_supported=sum(1 for row in rows if row.get("support_status") == "supported"),
        files_unsupported=sum(1 for row in rows if row.get("support_status") != "supported"),
        scan_metrics=scan_summary.as_dict(),
    )
    last_run_id: str | None = None

    def emit(stage: str, value: float, **metadata: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(stage, value, **metadata)
        except TypeError:
            progress_callback(stage, value)

    def scoped_callback(index: int, row: Mapping[str, Any]):
        def callback(stage: str, value: float, **metadata: Any) -> None:
            emit(
                stage,
                (index + max(0.0, min(1.0, float(value)))) / max(1, total),
                current_file=str(row.get("relative_path") or ""),
                current_file_id=str(row.get("file_id") or ""),
                completed=index,
                total=total,
                current_page=metadata.get("current_page"),
                current_page_total=metadata.get("total_pages"),
                current_substage=metadata.get("current_substage"),
                run_id=metadata.get("run_id"),
            )
        return callback

    for index, row in enumerate(rows):
        check_cancel(cancel_event)
        file_id = str(row.get("file_id") or "")
        relative_path = str(row.get("relative_path") or "")
        if row.get("support_status") != "supported":
            emit(
                "skipped",
                (index + 1) / max(1, total),
                current_file=relative_path,
                current_file_id=file_id,
                completed=index + 1,
                total=total,
                current_substage="unsupported format",
            )
            continue
        callback = scoped_callback(index, row)
        with registry_write_mutex(registry_path):
            extraction = extract_unified(
                source,
                workers=min(workers, 2),
                force=force,
                registry_path=registry_path,
                workspace_root=workspace_root,
                _scan_summary=scan_summary,
                file_ids={file_id},
                _skip_catalog_summary=True,
                _skip_recovery=True,
                cancel_event=cancel_event,
                progress_callback=callback,
                vision_mode=vision_mode,
                vision_provider=vision_provider,
                registry=registry,
            )
            last_run_id = extraction.run_id
            emit(
                "clean",
                (index + 0.52) / max(1, total),
                current_file=relative_path,
                current_file_id=file_id,
                completed=index,
                total=total,
                current_substage="准备确定性清洗",
                run_id=extraction.run_id,
            )
            if vision_mode == "ai_vision":
                emit(
                    "clean",
                    (index + 0.52) / max(1, total),
                    current_file=relative_path,
                    current_file_id=file_id,
                    completed=index,
                    total=total,
                    current_substage="cleaning_assets",
                    run_id=extraction.run_id,
                )
            cleaned = clean_source(
                source,
                workers=workers,
                force=force,
                drop_exact_duplicates=drop_exact_duplicates,
                registry_path=registry_path,
                workspace_root=workspace_root,
                extraction_summary=extraction,
                file_ids={file_id},
                _skip_catalog_summary=True,
                _skip_recovery=True,
                progress_callback=callback,
                cancel_event=cancel_event,
                registry=registry,
            )
        aggregate.extracted += extraction.processed
        aggregate.reused_extraction += extraction.reused
        aggregate.extraction_failures += extraction.failed
        aggregate.cleaned += cleaned.cleaned
        aggregate.reused_cleaning += cleaned.reused_cleaning
        aggregate.cleaning_failures += cleaned.cleaning_failures
        aggregate.normalize_ms += cleaned.normalize_ms
        aggregate.profile_ms += cleaned.profile_ms
        aggregate.parquet_write_ms += cleaned.parquet_write_ms
        aggregate.artifact_write_ms += cleaned.artifact_write_ms
        aggregate.duckdb_write_ms += cleaned.duckdb_write_ms
        emit(
            "local_ready",
            (index + 1) / max(1, total),
            current_file=relative_path,
            current_file_id=file_id,
            completed=index + 1,
            total=total,
            current_substage="Catalog / Search ready",
            run_id=extraction.run_id,
        )

    catalog, rows_count, chars = _summary_catalog(registry, source_root)
    aggregate.table_assets = int(catalog.get("table_assets", 0))
    aggregate.text_assets = int(catalog.get("text_assets", 0))
    aggregate.ready = int(catalog.get("ready", 0))
    aggregate.needs_review = int(catalog.get("needs_review", 0))
    aggregate.unusable = int(catalog.get("unusable", 0))
    aggregate.quality_issues = int(catalog.get("quality_issues", 0))
    aggregate.semantic_pending = int(catalog.get("semantic_pending", 0))
    aggregate.rows = rows_count
    aggregate.chars = chars
    aggregate.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    aggregate.cleaning_wall_time_ms = aggregate.wall_time_ms
    emit("completed", 1.0, completed=total, total=total, current_substage="local processing complete", run_id=last_run_id)
    return aggregate


def process_source(
    source: Path | str,
    *,
    workers: int | None = None,
    force: bool = False,
    drop_exact_duplicates: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
    vision_mode: str = "local",
    vision_provider: Any | None = None,
    scan_batch_size: int | None = None,
) -> CleaningSummary:
    """Run scan, independent extraction, deterministic cleaning, and profiling."""

    wall_started = time.perf_counter_ns()
    worker_count = normalize_cleaning_workers(workers)
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    if not registry_file.is_file():
        from dongjian.registry import Registry

        Registry.ensure_initialized(registry_file)

    return _process_source_incremental(
        source,
        workers=worker_count,
        force=force,
        drop_exact_duplicates=drop_exact_duplicates,
        registry_path=registry_file,
        workspace_root=workspace,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        vision_mode=vision_mode,
        vision_provider=vision_provider,
        _scan_batch_size=scan_batch_size,
    )

    def emit(stage: str, value: float, **metadata: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(stage, value, **metadata)
        except TypeError:
            # Preserve the small callback contract used by standalone callers.
            progress_callback(stage, value)

    check_cancel(cancel_event)
    if progress_callback is not None:
        emit("scan", 0.02, current_substage="扫描文件")
    extraction = extract_unified(
        source,
        workers=min(worker_count, 2),
        force=force,
        registry_path=registry_file,
        workspace_root=workspace,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        vision_mode=vision_mode,
        vision_provider=vision_provider,
    )
    check_cancel(cancel_event)
    emit(
        "extract",
        0.30,
        completed=extraction.processed,
        total=extraction.files_discovered,
        current_substage="routes complete",
        run_id=extraction.run_id,
    )
    emit("clean", 0.45, completed=extraction.processed, total=extraction.files_discovered, current_substage="准备确定性清洗")
    if vision_mode == "ai_vision":
        emit(
            "clean",
            0.45,
            completed=extraction.processed,
            total=extraction.files_discovered,
            current_substage="cleaning_assets",
        )
    summary = clean_source(
        source,
        workers=worker_count,
        force=force,
        drop_exact_duplicates=drop_exact_duplicates,
        registry_path=registry_file,
        workspace_root=workspace,
        extraction_summary=extraction,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    check_cancel(cancel_event)
    summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    return summary
