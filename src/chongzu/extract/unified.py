"""Formal Phase 5B extraction entry point.

The coordinator owns one registry scan and then invokes the independent
structured, native-PDF, OCR/image-table, and plain-text routes.  A route may
produce zero assets, many assets, or an isolated failure without changing the
other routes for the same file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
import time
from typing import Any
from uuid import uuid4

from chongzu import paths
from chongzu.cancellation import check_cancel
from chongzu.registry import Registry, canonical_source_root
from chongzu.scan import ScanError, scan_source

from .pdf.runner import PDFExtractionSummary, extract_pdf
from .pdf.table_runner import PDFTableExtractionSummary, extract_pdf_tables
from .structured import StructuredExtractionSummary, extract_structured
from .ocr.runner import OCRExtractionSummary, extract_ocr
from .text import TextExtractionSummary, extract_text


class UnifiedExtractionError(RuntimeError):
    pass


MAX_UNIFIED_WORKERS = 2


def normalize_unified_workers(workers: int | None) -> int:
    value = 1 if workers is None else int(workers)
    if value < 1 or value > MAX_UNIFIED_WORKERS:
        raise ValueError(f"unified workers must be between 1 and {MAX_UNIFIED_WORKERS}")
    return value


@dataclass
class UnifiedExtractionSummary:
    run_id: str
    source_root: str
    files_discovered: int = 0
    supported: int = 0
    unsupported: int = 0
    processed: int = 0
    reused: int = 0
    failed: int = 0
    table_assets: int = 0
    text_assets: int = 0
    text_chunks: int = 0
    quality_issues: int = 0
    structured_files: int = 0
    native_pdf_pages: int = 0
    ocr_pages_images: int = 0
    deferred: int = 0
    scan_ms: float = 0.0
    extraction_ms: float = 0.0
    wall_time_ms: float = 0.0
    route_timings: dict[str, float] = field(default_factory=dict)
    structured_summary: StructuredExtractionSummary | None = field(default=None, repr=False)
    pdf_summary: PDFExtractionSummary | None = field(default=None, repr=False)
    pdf_table_summary: PDFTableExtractionSummary | None = field(default=None, repr=False)
    ocr_summary: OCRExtractionSummary | None = field(default=None, repr=False)
    vision_summary: Any | None = field(default=None, repr=False)
    vision_pdf_summary: Any | None = field(default=None, repr=False)
    text_summary: TextExtractionSummary | None = field(default=None, repr=False)
    vision_mode: str = "local"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_root": self.source_root,
            "files_discovered": self.files_discovered,
            "supported": self.supported,
            "unsupported": self.unsupported,
            "processed": self.processed,
            "reused": self.reused,
            "failed": self.failed,
            "table_assets": self.table_assets,
            "text_assets": self.text_assets,
            "text_chunks": self.text_chunks,
            "quality_issues": self.quality_issues,
            "structured_files": self.structured_files,
            "native_pdf_pages": self.native_pdf_pages,
            "ocr_pages_images": self.ocr_pages_images,
            "deferred": self.deferred,
            "scan_ms": self.scan_ms,
            "extraction_ms": self.extraction_ms,
            "wall_time_ms": self.wall_time_ms,
            "route_timings": dict(self.route_timings),
            "vision_mode": self.vision_mode,
        }


def _rows(registry: Registry, source_root: str) -> list[dict[str, Any]]:
    return registry.list_files(source_root, state="present", limit=100_000)


def _latest_route_statuses(registry: Registry, file_id: str, content_sha256: str) -> dict[str, str]:
    cursor = registry.connection.execute(
        """
        SELECT attempted_route, status
        FROM extraction_runs
        WHERE file_id=? AND content_sha256=?
        ORDER BY finished_at DESC NULLS LAST, started_at DESC
        """,
        [file_id, content_sha256],
    )
    statuses: dict[str, str] = {}
    for route, status in cursor.fetchall():
        statuses.setdefault(str(route), str(status))
    return statuses


def _processing_counts(registry: Registry, rows: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    """Return processed, failed, deferred, and route-success counts.

    Counts are file-oriented.  A PDF can have several successful extraction
    routes and still counts once; stage-level reuse remains visible separately
    on the summary.
    """

    if not rows:
        return 0, 0, 0, 0
    source_roots = {str(row.get("source_root") or "") for row in rows}
    if len(source_roots) == 1:
        return registry.processing_counts(next(iter(source_roots)))
    # This branch is retained for callers that intentionally pass a mixed
    # source list (the production coordinator passes one directory). It keeps
    # the original semantics without changing the public helper contract.
    processed = failed = deferred = 0
    for row in rows:
        if str(row.get("support_status")) != "supported":
            continue
        sha = row.get("sha256")
        if not sha:
            failed += 1
            continue
        statuses = _latest_route_statuses(registry, str(row["file_id"]), str(sha))
        fmt = str(row.get("business_format") or "")
        if fmt in {"csv", "tsv", "xls", "xlsx"}:
            expected = statuses.get("structured_native")
        elif fmt == "pdf":
            native_status = statuses.get("pdf_native_text")
            ocr_status = statuses.get("ocr_rapidocr")
            expected = (
                "failed"
                if native_status == "failed" or ocr_status == "failed"
                else "successful"
                if native_status in {"successful", "partial"} or ocr_status in {"successful", "partial"}
                else native_status or ocr_status
            )
        elif fmt in {"jpeg", "png"}:
            # An explicitly selected Vision run supersedes the local image
            # route for the same content.  The fallback branch is used only
            # for callers that intentionally pass a mixed source list; keep
            # it aligned with Registry.processing_counts().
            expected = statuses.get("vision_llm") or statuses.get("ocr_rapidocr")
        elif fmt == "txt":
            expected = statuses.get("text_plain")
        else:
            expected = None
        if expected in {"successful", "partial"}:
            processed += 1
        elif expected == "failed":
            failed += 1
        else:
            deferred += 1
    return processed, failed, deferred, processed + failed


def _run_route(name: str, callback) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    value = callback()
    return value, (time.perf_counter_ns() - started) / 1_000_000


def extract_unified(
    source: Path | str,
    *,
    workers: int | None = None,
    force: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    cancel_event: Event | None = None,
    progress_callback=None,
    vision_mode: str = "local",
    vision_provider: Any | None = None,
) -> UnifiedExtractionSummary:
    """Run all currently implemented deterministic extraction routes once."""

    wall_started = time.perf_counter_ns()
    from chongzu.vision.pdf_runner import extract_vision_pdf
    from chongzu.vision.runner import extract_vision, normalize_vision_mode

    vision_mode = normalize_vision_mode(vision_mode)
    worker_count = normalize_unified_workers(workers)
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    check_cancel(cancel_event)
    try:
        scan_summary = scan_source(
            source,
            workers=worker_count,
            registry_path=registry_file,
            cancel_event=cancel_event,
        )
    except ScanError as exc:
        raise UnifiedExtractionError(str(exc)) from exc
    source_root = canonical_source_root(source, require_directory=True)
    summary = UnifiedExtractionSummary(
        run_id=f"extract_{uuid4().hex}",
        source_root=source_root,
        files_discovered=scan_summary.discovered_count,
        scan_ms=scan_summary.elapsed_ms,
        vision_mode=vision_mode,
    )

    registry = Registry.open(registry_file, initialize=False)
    try:
        rows = _rows(registry, source_root)
    finally:
        registry.close()
    summary.supported = sum(1 for row in rows if row.get("support_status") == "supported")
    summary.unsupported = sum(1 for row in rows if row.get("support_status") != "supported")
    formats = {str(row.get("business_format") or "") for row in rows if row.get("support_status") == "supported"}
    summary.structured_files = sum(
        1 for row in rows if row.get("support_status") == "supported" and row.get("business_format") in {"csv", "tsv", "xls", "xlsx"}
    )

    route_kwargs = {
        "workers": worker_count,
        "force": force,
        "registry_path": registry_file,
        "workspace_root": workspace,
        "_scan_summary": scan_summary,
        "progress_callback": progress_callback,
        "cancel_event": cancel_event,
    }
    # PDF rendering, table reconstruction, and OCR all load native models or
    # large page buffers.  Keep those heavy routes to one process inside a
    # single product task; structured/cleaning concurrency remains separately
    # bounded.  This prevents a mixed scanned-PDF + image directory from
    # starting several native runtimes at once and exhausting Windows process
    # memory, while preserving the direct benchmark APIs' worker controls.
    heavy_route_kwargs = {**route_kwargs, "workers": 1}
    if formats & {"csv", "tsv", "xls", "xlsx"}:
        check_cancel(cancel_event)
        summary.structured_summary, elapsed = _run_route(
            "structured", lambda: extract_structured(source, **route_kwargs)
        )
        summary.route_timings["structured"] = elapsed
    if "pdf" in formats:
        check_cancel(cancel_event)
        summary.pdf_summary, elapsed = _run_route(
            "pdf", lambda: extract_pdf(source, **heavy_route_kwargs)
        )
        summary.route_timings["pdf"] = elapsed
        summary.native_pdf_pages = summary.pdf_summary.pages
        check_cancel(cancel_event)
        summary.pdf_table_summary, elapsed = _run_route(
            "pdf_table", lambda: extract_pdf_tables(source, **heavy_route_kwargs)
        )
        summary.route_timings["pdf_table"] = elapsed
    if formats & {"pdf", "jpeg", "png"}:
        check_cancel(cancel_event)
        summary.ocr_summary, elapsed = _run_route(
            "ocr",
            lambda: extract_ocr(
                source,
                **heavy_route_kwargs,
                include_images=vision_mode != "ai_vision",
                include_pdfs=vision_mode != "ai_vision",
            ),
        )
        summary.route_timings["ocr"] = elapsed
        summary.ocr_pages_images = summary.ocr_summary.pages_ocred + summary.ocr_summary.images_considered
    if vision_mode == "ai_vision" and formats & {"pdf", "jpeg", "png"}:
        if vision_provider is None:
            raise UnifiedExtractionError("AI Vision is selected but no verified Vision provider is available")
        if formats & {"jpeg", "png"}:
            check_cancel(cancel_event)
            summary.vision_summary, elapsed = _run_route(
                "vision",
                lambda: extract_vision(
                    source,
                    provider=vision_provider,
                    **heavy_route_kwargs,
                ),
            )
            summary.route_timings["vision"] = elapsed
        if "pdf" in formats:
            check_cancel(cancel_event)
            summary.vision_pdf_summary, elapsed = _run_route(
                "vision_pdf",
                lambda: extract_vision_pdf(
                    source,
                    provider=vision_provider,
                    **heavy_route_kwargs,
                ),
            )
            summary.route_timings["vision_pdf"] = elapsed
            if summary.vision_summary is None:
                summary.vision_summary = summary.vision_pdf_summary
    if "txt" in formats:
        check_cancel(cancel_event)
        summary.text_summary, elapsed = _run_route(
            "text", lambda: extract_text(source, **route_kwargs)
        )
        summary.route_timings["text"] = elapsed

    check_cancel(cancel_event)
    registry = Registry.open(registry_file, initialize=False)
    try:
        current_rows = _rows(registry, source_root)
        processed, failed, deferred, _ = _processing_counts(registry, current_rows)
        catalog = registry.catalog_summary(source_root)
    finally:
        registry.close()
    summary.processed = processed
    summary.failed = failed
    summary.deferred = deferred
    seen_stage_ids: set[int] = set()
    summary.reused = 0
    for stage in (
        summary.structured_summary,
        summary.pdf_summary,
        summary.pdf_table_summary,
        summary.ocr_summary,
        summary.vision_summary,
        summary.vision_pdf_summary,
        summary.text_summary,
    ):
        if stage is None or id(stage) in seen_stage_ids:
            continue
        seen_stage_ids.add(id(stage))
        summary.reused += int(getattr(stage, "reused", 0) or 0)
    summary.table_assets = int(catalog.get("table_assets", 0))
    summary.text_assets = int(catalog.get("text_assets", 0))
    summary.text_chunks = int(catalog.get("text_chunks", 0))
    summary.quality_issues = int(catalog.get("quality_issues", 0))
    summary.extraction_ms = sum(summary.route_timings.values())
    summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    return summary
