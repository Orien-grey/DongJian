"""Single-writer orchestration for deterministic cleaning and profiling."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
from pathlib import Path
from threading import Event
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from chongzu import paths
from chongzu.cancellation import check_cancel
from chongzu.extract.unified import UnifiedExtractionSummary, extract_unified
from chongzu.registry import Registry, canonical_source_root, utc_now

from .models import CleaningAssetResult, CleaningSummary
from .quality import assess_table_quality, assess_text_quality, cleaning_failure_issue
from .table_cleaner import CLEANER_NAME as TABLE_CLEANER, clean_table
from .text_cleaner import CLEANER_NAME as TEXT_CLEANER, clean_text
from ..extract.artifacts import artifact_absolute


MAX_CLEANING_WORKERS = 4
ProgressCallback = Callable[..., None]


class CleaningError(RuntimeError):
    """Raised when the cleaning coordinator cannot prepare its source."""


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
    extraction_summary: UnifiedExtractionSummary | None = None,
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

    registry = Registry.open(registry_file, initialize=False)
    try:
        check_cancel(cancel_event)
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
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="chongzu-clean")
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
        registry.close()
    if progress_callback is not None:
        progress_callback("profile", 0.78)
    summary.cleaning_wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    summary.wall_time_ms = summary.cleaning_wall_time_ms
    registry = Registry.open(registry_file, initialize=False)
    try:
        catalog, rows, chars = _summary_catalog(registry, source_root)
    finally:
        registry.close()
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
) -> CleaningSummary:
    """Run scan, independent extraction, deterministic cleaning, and profiling."""

    wall_started = time.perf_counter_ns()
    worker_count = normalize_cleaning_workers(workers)
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    if not registry_file.is_file():
        from chongzu.registry import Registry

        Registry.ensure_initialized(registry_file)

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
