"""Registry-backed PDF extraction coordinator and benchmark summary."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from chongzu import paths
from chongzu.registry import Registry, canonical_source_root, utc_now
from chongzu.scan import ScanError, scan_source

from ..models import StructuredSource
from .artifacts import artifact_absolute
from .pymupdf_extractor import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    PdfExtractionResult,
    extract_pdf_file,
)


MAX_PDF_WORKERS = 4


class PDFExtractionError(RuntimeError):
    pass


def normalize_pdf_workers(workers: int | None) -> int:
    value = 1 if workers is None else int(workers)
    if value < 1 or value > MAX_PDF_WORKERS:
        raise ValueError(f"PDF workers must be between 1 and {MAX_PDF_WORKERS}")
    return value


def pdf_extraction_identity(source: StructuredSource) -> str:
    payload = json.dumps(
        {
            "file_id": source.file_id,
            "content_sha256": source.content_sha256,
            "business_format": source.business_format,
            "extractor": EXTRACTOR_NAME,
            "extractor_version": EXTRACTOR_VERSION,
            "pdf_config_version": paths.PDF_CONFIG_VERSION,
            "text_chunk_config_version": paths.TEXT_CHUNK_CONFIG_VERSION,
            "registry_schema_version": paths.EXTRACTION_IDENTITY_SCHEMA_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_from_row(row: dict[str, object], workspace_root: Path) -> StructuredSource:
    return StructuredSource(
        file_id=str(row["file_id"]),
        content_sha256=str(row["sha256"]),
        source_root=str(row["source_root"]),
        relative_path=str(row["relative_path"]),
        business_format="pdf",
        size_bytes=int(row["size_bytes"] or 0),
        mtime_ns=int(row["mtime_ns"] or 0),
        workspace_root=workspace_root,
    )


def _artifacts_exist(reusable: dict[str, object], workspace_root: Path) -> bool:
    artifacts = [str(item) for item in reusable.get("artifacts", []) if item]
    if not artifacts:
        return False
    try:
        return all(artifact_absolute(item, workspace_root).is_file() for item in artifacts)
    except (OSError, ValueError):
        return False


def _extract_one(source: StructuredSource, run_id: str, identity: str) -> PdfExtractionResult:
    try:
        before = source.path.stat()
    except OSError as exc:
        return PdfExtractionResult(
            source=source,
            extraction_run_id=run_id,
            extraction_identity=identity,
            status="failed",
            error_category="source_stat_error",
            error_message=str(exc),
        )
    if (before.st_size, before.st_mtime_ns) != (source.size_bytes, source.mtime_ns):
        return PdfExtractionResult(
            source=source,
            extraction_run_id=run_id,
            extraction_identity=identity,
            status="failed",
            error_category="source_changed_after_scan",
            error_message="source size or mtime changed after the automatic registry scan",
        )
    result = extract_pdf_file(source, run_id, identity)
    try:
        after = source.path.stat()
    except OSError as exc:
        result.status = "failed"
        result.text_assets.clear()
        result.text_chunks.clear()
        result.issues.clear()
        result.profile = None
        result.profile_artifact_path = None
        result.error_category = "source_stat_error"
        result.error_message = str(exc)
        return result
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        result.status = "failed"
        result.text_assets.clear()
        result.text_chunks.clear()
        result.issues.clear()
        result.profile = None
        result.profile_artifact_path = None
        result.error_category = "source_changed_during_extraction"
        result.error_message = "source size or mtime changed while it was being extracted"
    return result


@dataclass
class PDFExtractionSummary:
    source_root: str
    files_considered: int = 0
    pdf_files: int = 0
    extracted: int = 0
    reused: int = 0
    pages: int = 0
    total_bytes: int = 0
    total_chars: int = 0
    text_assets_produced: int = 0
    quality_issues: int = 0
    native_text_pdfs: int = 0
    mixed_pdfs: int = 0
    suspected_scanned_pdfs: int = 0
    unknown_pdfs: int = 0
    failed_pdfs: int = 0
    discovery_scan_ms: float = 0.0
    text_extraction_ms: float = 0.0
    profiling_ms: float = 0.0
    artifact_write_ms: float = 0.0
    registry_write_ms: float = 0.0
    wall_time_ms: float = 0.0

    def add_profile(self, profile: dict[str, object]) -> None:
        classification = str(profile.get("classification") or "unknown")
        if classification == "native_text":
            self.native_text_pdfs += 1
        elif classification == "mixed":
            self.mixed_pdfs += 1
        elif classification == "suspected_scanned":
            self.suspected_scanned_pdfs += 1
        else:
            self.unknown_pdfs += 1
        self.pages += int(profile.get("page_count") or 0)
        self.total_chars += int(profile.get("total_chars") or 0)

    def benchmark_metrics(self) -> dict[str, float | int | str]:
        seconds = self.wall_time_ms / 1000.0 if self.wall_time_ms else 0.0
        megabytes = self.total_bytes / (1024 * 1024)
        return {
            "PDF files": self.pdf_files,
            "pages": self.pages,
            "total bytes": self.total_bytes,
            "pages/sec": self.pages / seconds if seconds else 0.0,
            "MB/sec": megabytes / seconds if seconds else 0.0,
            "chars extracted": self.total_chars,
            "native-text PDFs": self.native_text_pdfs,
            "mixed PDFs": self.mixed_pdfs,
            "suspected-scanned PDFs": self.suspected_scanned_pdfs,
            "unknown PDFs": self.unknown_pdfs,
            "failed PDFs": self.failed_pdfs,
            "text extraction ms": self.text_extraction_ms,
            "profiling ms": self.profiling_ms,
            "artifact write ms": self.artifact_write_ms,
            "registry write ms": self.registry_write_ms,
            "wall clock ms": self.wall_time_ms,
            "peak memory": "not collected (no zero-cost reliable cross-process metric)",
        }


def _record_result(
    *,
    registry: Registry,
    summary: PDFExtractionSummary,
    result: PdfExtractionResult,
    started_at,
    force: bool,
) -> None:
    finished_at = utc_now()
    write_started = time.perf_counter_ns()
    registry.record_pdf_result(
        result,
        started_at=started_at,
        finished_at=finished_at,
        force=force,
    )
    summary.registry_write_ms += (time.perf_counter_ns() - write_started) / 1_000_000
    summary.text_extraction_ms += result.timings.text_extraction_ms
    summary.profiling_ms += result.timings.profiling_ms
    summary.artifact_write_ms += result.timings.artifact_write_ms
    summary.quality_issues += len(result.issues)
    if result.status == "failed":
        summary.failed_pdfs += 1
        return
    summary.extracted += 1
    summary.text_assets_produced += len(result.text_assets)
    if result.profile is not None:
        summary.add_profile(result.profile.as_dict())


def extract_pdf(
    source: Path | str,
    *,
    workers: int | None = None,
    force: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    _scan_summary=None,
) -> PDFExtractionSummary:
    """Scan *source*, then extract current PDF candidates with PyMuPDF."""

    wall_started = time.perf_counter_ns()
    worker_count = normalize_pdf_workers(workers)
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if _scan_summary is None:
        try:
            scan_summary = scan_source(source, workers=worker_count, registry_path=registry_file)
        except ScanError as exc:
            raise PDFExtractionError(str(exc)) from exc
    else:
        scan_summary = _scan_summary
    source_root = canonical_source_root(source, require_directory=True)
    summary = PDFExtractionSummary(
        source_root=source_root,
        discovery_scan_ms=scan_summary.elapsed_ms,
    )
    registry = Registry.open(registry_file)
    try:
        registry.recover_incomplete_extractions(source_root)
        rows = registry.pdf_candidates(source_root)
        summary.files_considered = registry.count_present_files(source_root)
        summary.pdf_files = len(rows)
        summary.total_bytes = sum(int(row["size_bytes"] or 0) for row in rows)
        sources = iter(_source_from_row(row, workspace) for row in rows)

        if worker_count == 1:
            for pdf_source in sources:
                identity = pdf_extraction_identity(pdf_source)
                reusable = None if force else registry.reusable_pdf_extraction(identity)
                if reusable is not None and _artifacts_exist(reusable, workspace):
                    summary.reused += 1
                    summary.add_profile(reusable.get("profile") or {})
                    continue
                run_id = f"xrun_{uuid4().hex}"
                started_at = utc_now()
                write_started = time.perf_counter_ns()
                registry.start_pdf_extraction(
                    extraction_run_id=run_id,
                    extraction_identity=identity,
                    source=pdf_source,
                    extractor=EXTRACTOR_NAME,
                    extractor_version=EXTRACTOR_VERSION,
                    started_at=started_at,
                    force=force,
                )
                summary.registry_write_ms += (time.perf_counter_ns() - write_started) / 1_000_000
                result = _extract_one(pdf_source, run_id, identity)
                _record_result(
                    registry=registry,
                    summary=summary,
                    result=result,
                    started_at=started_at,
                    force=force,
                )
        else:
            pending: dict[
                Future[PdfExtractionResult],
                tuple[StructuredSource, datetime, bool, str, str],
            ] = {}
            exhausted = False
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                while pending or not exhausted:
                    while not exhausted and len(pending) < worker_count * 2:
                        try:
                            pdf_source = next(sources)
                        except StopIteration:
                            exhausted = True
                            break
                        identity = pdf_extraction_identity(pdf_source)
                        reusable = None if force else registry.reusable_pdf_extraction(identity)
                        if reusable is not None and _artifacts_exist(reusable, workspace):
                            summary.reused += 1
                            summary.add_profile(reusable.get("profile") or {})
                            continue
                        run_id = f"xrun_{uuid4().hex}"
                        started_at = utc_now()
                        write_started = time.perf_counter_ns()
                        registry.start_pdf_extraction(
                            extraction_run_id=run_id,
                            extraction_identity=identity,
                            source=pdf_source,
                            extractor=EXTRACTOR_NAME,
                            extractor_version=EXTRACTOR_VERSION,
                            started_at=started_at,
                            force=force,
                        )
                        summary.registry_write_ms += (time.perf_counter_ns() - write_started) / 1_000_000
                        future = executor.submit(_extract_one, pdf_source, run_id, identity)
                        pending[future] = (pdf_source, started_at, force, run_id, identity)
                    if not pending:
                        continue
                    done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                    for future in done:
                        pdf_source, started_at, was_forced, run_id, identity = pending.pop(future)
                        try:
                            result = future.result()
                        except Exception as exc:  # isolate one native parser/worker
                            result = PdfExtractionResult(
                                source=pdf_source,
                                extraction_run_id=run_id,
                                extraction_identity=identity,
                                status="failed",
                                error_category="worker_error",
                                error_message=str(exc),
                            )
                        _record_result(
                            registry=registry,
                            summary=summary,
                            result=result,
                            started_at=started_at,
                            force=was_forced,
                        )
    finally:
        registry.close()
    summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    return summary
