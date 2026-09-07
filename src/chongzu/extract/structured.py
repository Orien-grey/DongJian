"""Bounded structured extraction coordinated around the file registry."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import time
from typing import Callable
from uuid import uuid4

from chongzu import paths
from chongzu.cancellation import check_cancel
from chongzu.registry import Registry, canonical_source_root, utc_now
from chongzu.scan import ScanError, scan_source

from .artifacts import artifact_absolute
from .csv_extractor import EXTRACTOR_NAME as CSV_EXTRACTOR, extract_csv
from .excel_extractor import EXTRACTOR_NAME as EXCEL_EXTRACTOR, extract_excel
from .models import FileExtractionResult, StructuredExtractionSummary, StructuredSource


MAX_STRUCTURED_WORKERS = 8


class StructuredExtractionError(RuntimeError):
    pass


def normalize_workers(workers: int | None) -> int:
    if workers is None:
        return max(1, min(4, os.cpu_count() or 1))
    if workers < 1 or workers > MAX_STRUCTURED_WORKERS:
        raise ValueError(f"workers must be between 1 and {MAX_STRUCTURED_WORKERS}")
    return workers


CSV_EXTRACTOR_VERSION = version("polars")
EXCEL_EXTRACTOR_VERSION = version("python-calamine")


def _extractor(format_name: str) -> tuple[str, str]:
    if format_name in {"csv", "tsv"}:
        return CSV_EXTRACTOR, CSV_EXTRACTOR_VERSION
    return EXCEL_EXTRACTOR, EXCEL_EXTRACTOR_VERSION


def extraction_identity(source: StructuredSource, extractor: str, extractor_version: str) -> str:
    payload = json.dumps(
        {
            "file_id": source.file_id,
            "content_sha256": source.content_sha256,
            "extractor": extractor,
            "extractor_version": extractor_version,
            "structured_config_version": paths.STRUCTURED_CONFIG_VERSION,
            "registry_schema_version": paths.EXTRACTION_IDENTITY_SCHEMA_VERSION,
            "business_format": source.business_format,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _extract_one(source: StructuredSource, run_id: str, identity: str) -> FileExtractionResult:
    before = source.path.stat()
    if (before.st_size, before.st_mtime_ns) != (source.size_bytes, source.mtime_ns):
        extractor, extractor_version = _extractor(source.business_format)
        return FileExtractionResult(
            source=source,
            extraction_run_id=run_id,
            extraction_identity=identity,
            extractor=extractor,
            extractor_version=extractor_version,
            status="failed",
            error_category="source_changed_after_scan",
            error_message="source size or mtime changed after the automatic registry scan",
        )
    if source.business_format in {"csv", "tsv"}:
        result = extract_csv(source, run_id, identity)
    else:
        result = extract_excel(source, run_id, identity)
    after = source.path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        result.status = "failed"
        result.assets.clear()
        result.issues.clear()
        result.error_category = "source_changed_during_extraction"
        result.error_message = "source size or mtime changed while it was being extracted"
    return result


def _source_from_row(row: dict[str, object], workspace_root: Path) -> StructuredSource:
    return StructuredSource(
        file_id=str(row["file_id"]),
        content_sha256=str(row["sha256"]),
        source_root=str(row["source_root"]),
        relative_path=str(row["relative_path"]),
        business_format=str(row["business_format"]),
        size_bytes=int(row["size_bytes"] or 0),
        mtime_ns=int(row["mtime_ns"] or 0),
        workspace_root=workspace_root,
    )


def _artifacts_exist(reusable: dict[str, object], workspace_root: Path) -> bool:
    artifacts = reusable.get("artifacts", [])
    if int(reusable.get("table_count", 0) or 0) == 0:
        return True
    return bool(artifacts) and all(artifact_absolute(str(item), workspace_root).is_file() for item in artifacts)


def extract_structured(
    source: Path | str,
    *,
    workers: int | None = None,
    force: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    registry: Registry | None = None,
    _scan_summary=None,
    file_ids: set[str] | None = None,
    _skip_recovery: bool = False,
    progress_callback: Callable[..., None] | None = None,
    cancel_event=None,
) -> StructuredExtractionSummary:
    """Scan *source*, then extract current CSV/TSV/XLS/XLSX registry rows."""

    wall_started = time.perf_counter_ns()
    worker_count = normalize_workers(workers)
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if _scan_summary is None:
        try:
            scan_summary = scan_source(
                source,
                workers=worker_count,
                registry_path=registry_file,
                cancel_event=cancel_event,
            )
        except ScanError as exc:
            raise StructuredExtractionError(str(exc)) from exc
    else:
        scan_summary = _scan_summary
    source_root = canonical_source_root(source, require_directory=True)
    summary = StructuredExtractionSummary(source_root=source_root, discovery_scan_ms=scan_summary.elapsed_ms)
    owns_registry = registry is None
    registry = registry or Registry.open(registry_file, initialize=False)
    try:
        if not _skip_recovery:
            registry.recover_incomplete_extractions(source_root)
        rows = registry.structured_candidates(source_root)
        if file_ids is not None:
            rows = [row for row in rows if str(row.get("file_id") or "") in file_ids]
        summary.files_considered = registry.count_present_files(source_root)
        summary.structured_supported = len(rows)
        summary.total_bytes = sum(int(row["size_bytes"] or 0) for row in rows)
        completed = 0

        def emit(file_name: str | None, substage: str) -> None:
            if progress_callback is None:
                return
            progress = 0.30 + (0.15 * completed / max(1, len(rows)))
            try:
                progress_callback(
                    "extract",
                    progress,
                    current_file=file_name,
                    completed=completed,
                    total=len(rows),
                    current_substage=substage,
                )
            except TypeError:
                progress_callback("extract", progress)

        pending: dict[Future[FileExtractionResult], tuple[StructuredSource, str, datetime, bool]] = {}
        sources = iter(_source_from_row(row, workspace) for row in rows)
        exhausted = False
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="chongzu-structured")
        try:
            while pending or not exhausted:
                check_cancel(cancel_event)
                while not exhausted and len(pending) < worker_count * 2:
                    check_cancel(cancel_event)
                    try:
                        structured_source = next(sources)
                    except StopIteration:
                        exhausted = True
                        break
                    emit(structured_source.relative_path, "读取表格")
                    extractor, extractor_version = _extractor(structured_source.business_format)
                    identity = extraction_identity(structured_source, extractor, extractor_version)
                    reusable = None if force else registry.reusable_extraction(identity)
                    if reusable is not None and _artifacts_exist(reusable, workspace):
                        summary.reused += 1
                        summary.sheets += int(reusable.get("sheet_count", 0) or 0)
                        completed += 1
                        emit(structured_source.relative_path, "复用已有表格结果")
                        continue
                    run_id = f"xrun_{uuid4().hex}"
                    started_at = utc_now()
                    write_started = time.perf_counter_ns()
                    registry.start_structured_extraction(
                        extraction_run_id=run_id,
                        extraction_identity=identity,
                        source=structured_source,
                        extractor=extractor,
                        extractor_version=extractor_version,
                        started_at=started_at,
                        force=force,
                    )
                    summary.registry_write_ms += (time.perf_counter_ns() - write_started) / 1_000_000
                    future = executor.submit(_extract_one, structured_source, run_id, identity)
                    pending[future] = (structured_source, run_id, started_at, force)
                if not pending:
                    continue
                done, _ = wait(tuple(pending), timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    structured_source, run_id, started_at, was_forced = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # isolate one native parser/worker
                        extractor, extractor_version = _extractor(structured_source.business_format)
                        result = FileExtractionResult(
                            source=structured_source,
                            extraction_run_id=run_id,
                            extraction_identity=extraction_identity(structured_source, extractor, extractor_version),
                            extractor=extractor,
                            extractor_version=extractor_version,
                            status="failed",
                            error_category="worker_error",
                            error_message=str(exc),
                        )
                    finished_at = utc_now()
                    write_started = time.perf_counter_ns()
                    registry.record_structured_result(
                        result, started_at=started_at, finished_at=finished_at, force=was_forced
                    )
                    summary.registry_write_ms += (time.perf_counter_ns() - write_started) / 1_000_000
                    summary.workbook_open_ms += result.timings.workbook_open_ms
                    summary.extraction_ms += result.timings.extraction_ms
                    summary.normalization_ms += result.timings.normalization_ms
                    summary.parquet_write_ms += result.timings.parquet_write_ms
                    summary.sheets += result.sheet_count
                    summary.quality_issues += len(result.issues)
                    if result.status == "failed":
                        summary.failed += 1
                    else:
                        summary.extracted += 1
                        summary.tables_produced += len(result.assets)
                        summary.total_rows += result.total_rows
                    completed += 1
                    emit(structured_source.relative_path, "表格提取完成")
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
    summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    return summary
