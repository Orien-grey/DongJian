"""Page-scoped Vision extraction for clearly scanned PDF pages.

Native PDF inspection remains authoritative for routing.  This module only
renders pages that the persisted PyMuPDF profile identifies as image-only and
publishes the result through the existing Vision, asset, and Registry
contracts.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from threading import Event
from typing import Any, Callable
from uuid import uuid4

from chongzu import paths
from chongzu.assets import BoundingBox, SourceKind
from chongzu.cancellation import CancellationRequested, check_cancel
from chongzu.registry import Registry, canonical_source_root, utc_now as registry_now
from chongzu.scan import ScanError, scan_source

from ..extract.models import StructuredSource
from ..extract.pdf.runner import extract_pdf
from .contract import (
    VISION_CONTRACT_VERSION,
    VISION_EXTRACTOR,
    VISION_EXTRACTOR_VERSION,
    VISION_OUTPUT_CONTRACT,
)
from .models import VisionRequest
from .provider import VisionProvider, VisionProviderError
from .runner import (
    MAX_VISION_IMAGE_BYTES,
    VisionExtractionError,
    VisionExtractionResult,
    VisionExtractionSummary,
    _artifacts_exist,
    _image_identity,
    _provider_value,
    _quality_issue,
    _source_from_row,
    _write_table_asset,
    _write_text_asset,
)
from .validator import VisionContractError, validate_vision_payload


RENDER_DPI = 200


def pdf_vision_extraction_identity(
    source: StructuredSource,
    provider: VisionProvider,
    page_numbers: list[int],
) -> str:
    capabilities = getattr(provider, "capabilities", None)
    payload = {
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "business_format": source.business_format,
        "extractor": VISION_EXTRACTOR,
        "extractor_version": VISION_EXTRACTOR_VERSION,
        "pipeline_version": paths.VISION_PDF_PIPELINE_VERSION,
        "config_version": paths.VISION_PDF_CONFIG_VERSION,
        "provider_contract": _provider_value(capabilities, "contract", VISION_CONTRACT_VERSION),
        "provider_endpoint": _provider_value(provider, "endpoint", "provider"),
        "model": _provider_value(provider, "model", "unknown"),
        "render_dpi": RENDER_DPI,
        "page_numbers": list(page_numbers),
        "registry_schema_version": paths.EXTRACTION_IDENTITY_SCHEMA_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _selected_pages(profile: dict[str, Any] | None) -> list[int]:
    if not isinstance(profile, dict) or not isinstance(profile.get("pages"), list):
        return []
    selected: list[int] = []
    for value in profile["pages"]:
        if not isinstance(value, dict):
            continue
        try:
            page_number = int(value.get("page_number") or 0)
        except (TypeError, ValueError):
            continue
        # M2 deliberately handles only the unambiguous profile signal.  Pages
        # with native text remain on the native route, even in a mixed PDF.
        if page_number > 0 and bool(value.get("suspected_scanned")) and not bool(value.get("native_text_available")):
            selected.append(page_number)
    return selected


def _safe_error(provider: VisionProvider, value: object) -> str:
    """Keep provider failures useful without allowing a configured key out."""

    message = str(value).strip()
    config = getattr(provider, "config", None)
    secret = getattr(config, "api_key", "") if config is not None else ""
    if isinstance(secret, str) and secret:
        message = message.replace(secret, "[REDACTED]")
    return message[:4_000]


def _page_bbox(page: Any) -> BoundingBox:
    return BoundingBox(0.0, 0.0, float(page.rect.width), float(page.rect.height))


def _render_page(page: Any, pymupdf: Any) -> tuple[bytes, dict[str, Any]]:
    matrix = pymupdf.Matrix(RENDER_DPI / 72.0, RENDER_DPI / 72.0)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    image_bytes = pixmap.tobytes("png")
    if len(image_bytes) > MAX_VISION_IMAGE_BYTES:
        raise VisionExtractionError("rendered PDF page exceeds the local Vision size limit")
    return image_bytes, {
        "dpi": RENDER_DPI,
        "format": "png",
        "media_type": "image/png",
        "width_px": int(pixmap.width),
        "height_px": int(pixmap.height),
        "page_width": float(page.rect.width),
        "page_height": float(page.rect.height),
        "rotation": int(page.rotation or 0),
        "size_bytes": len(image_bytes),
        "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
    }


def _page_issue(
    source: StructuredSource,
    page_number: int,
    category: str,
    message: str,
) -> Any:
    asset_id = f"pdf_page:{source.file_id}:{page_number}"
    return _quality_issue(
        source,
        asset_id,
        category,
        {
            "page_number": page_number,
            "error": message,
            "source_sha256": source.content_sha256,
            "route": "scanned_pdf_page_vision",
        },
    )


def _emit(
    callback: Callable[..., None] | None,
    *,
    file_name: str,
    page_number: int | None,
    completed: int,
    total: int,
    substage: str,
) -> None:
    if callback is None:
        return
    progress = 0.58 + (0.18 * completed / max(1, total))
    display_file = file_name if page_number is None else f"{file_name} (page {page_number})"
    try:
        callback(
            "vision_extraction",
            progress,
            current_file=display_file,
            current_page=page_number,
            completed=completed,
            total=total,
            current_substage=substage,
        )
    except TypeError:
        callback("vision_extraction", progress)


def _record_page(
    registry: Registry,
    result: VisionExtractionResult,
    summary: VisionExtractionSummary,
    started_at: datetime,
    force: bool,
) -> None:
    started = time.perf_counter_ns()
    registry.record_vision_result(
        result,
        started_at=started_at,
        finished_at=registry_now(),
        force=force,
    )
    summary.registry_write_ms += (time.perf_counter_ns() - started) / 1_000_000


def _record_failed_file(
    registry: Registry,
    source: StructuredSource,
    run_id: str,
    identity: str,
    route_reason: str,
    provider: VisionProvider,
    started_at: datetime,
    force: bool,
    exc: Exception,
) -> VisionExtractionResult:
    result = VisionExtractionResult(
        source=source,
        extraction_run_id=run_id,
        extraction_identity=identity,
        route_reason=route_reason,
        pipeline_version=paths.VISION_PDF_PIPELINE_VERSION,
        configuration_version=paths.VISION_PDF_CONFIG_VERSION,
        status="failed",
        error_category="vision_pdf_extraction_error",
        error_message=_safe_error(provider, exc),
    )
    return result


def extract_vision_pdf(
    source: Path | str,
    *,
    provider: VisionProvider,
    workers: int | None = None,
    force: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    _scan_summary: Any | None = None,
    progress_callback: Callable[..., None] | None = None,
    cancel_event: Event | None = None,
) -> VisionExtractionSummary:
    """Run Vision once per clearly scanned PDF page, with resumable writes."""

    del workers
    wall_started = time.perf_counter_ns()
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if _scan_summary is None:
        try:
            scan_summary = scan_source(source, workers=1, registry_path=registry_file, cancel_event=cancel_event)
        except ScanError as exc:
            raise VisionExtractionError(str(exc)) from exc
    else:
        scan_summary = _scan_summary
    source_root = canonical_source_root(source, require_directory=True)
    summary = VisionExtractionSummary(source_root=source_root, discovery_scan_ms=scan_summary.elapsed_ms)
    probe_registry = Registry.open_reader(registry_file)
    try:
        # Direct callers may use this route without the unified coordinator;
        # create the canonical native profile before selecting pages.
        rows = probe_registry.pdf_candidates(source_root)
        needs_profile = rows and any(
            probe_registry.current_pdf_profile(str(row["file_id"]), str(row["sha256"])) is None
            for row in rows
        )
    finally:
        probe_registry.close()
    if needs_profile:
        extract_pdf(
            source,
            workers=1,
            force=force,
            registry_path=registry_file,
            workspace_root=workspace,
            _scan_summary=scan_summary,
            cancel_event=cancel_event,
        )
    registry = Registry.open(registry_file, initialize=False)
    try:
        rows = registry.pdf_candidates(source_root)

        selected: list[tuple[dict[str, Any], list[int], dict[str, Any]]] = []
        for row in rows:
            profile_record = registry.current_pdf_profile(str(row["file_id"]), str(row["sha256"]))
            profile = profile_record.get("profile") if profile_record else None
            page_numbers = _selected_pages(profile)
            if page_numbers:
                selected.append((row, page_numbers, profile or {}))
        summary.pdfs_considered = len(selected)
        summary.files_considered = len(selected)
        summary.pages_considered = sum(len(item[1]) for item in selected)
        if not selected:
            summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
            return summary
        if not bool(getattr(getattr(provider, "capabilities", None), "vision", False)):
            raise VisionExtractionError("configured provider does not declare Vision image capability")

        for row, page_numbers, profile in selected:
            check_cancel(cancel_event)
            source_item = _source_from_row(row, workspace)
            summary.pdfs_attempted += 1
            summary.files_attempted += 1
            route_reason = "scanned_pdf_page_vision"
            identity = pdf_vision_extraction_identity(source_item, provider, page_numbers)
            reusable = None if force else registry.reusable_vision_extraction(identity)
            if reusable is not None and (_artifacts_exist(reusable, workspace) or reusable.get("status") == "successful"):
                summary.reused += 1
                summary.pages_attempted += len(page_numbers)
                summary.pages_succeeded += len(page_numbers)
                _emit(
                    progress_callback,
                    file_name=source_item.relative_path,
                    page_number=None,
                    completed=summary.pages_attempted,
                    total=summary.pages_considered,
                    substage="reused_result",
                )
                continue

            run_id = f"vrun_{uuid4().hex}"
            started_at = registry_now()
            provider_contract = _provider_value(
                getattr(provider, "capabilities", None), "contract", VISION_CONTRACT_VERSION
            )
            provider_model = _provider_value(provider, "model", "unknown")
            registry.start_vision_extraction(
                extraction_run_id=run_id,
                extraction_identity=identity,
                source=source_item,
                extractor=VISION_EXTRACTOR,
                extractor_version=VISION_EXTRACTOR_VERSION,
                started_at=started_at,
                force=force,
                route_reason=route_reason,
                provider_contract=provider_contract,
                provider_model=provider_model,
                pipeline_version=paths.VISION_PDF_PIPELINE_VERSION,
                configuration_version=paths.VISION_PDF_CONFIG_VERSION,
            )
            result = VisionExtractionResult(
                source=source_item,
                extraction_run_id=run_id,
                extraction_identity=identity,
                route_reason=route_reason,
                pipeline_version=paths.VISION_PDF_PIPELINE_VERSION,
                configuration_version=paths.VISION_PDF_CONFIG_VERSION,
                pages_attempted=len(page_numbers),
            )
            document = None
            try:
                before = source_item.path.stat()
                if (before.st_size, before.st_mtime_ns) != (source_item.size_bytes, source_item.mtime_ns):
                    raise VisionExtractionError("source size or mtime changed after registry scan")
                import pymupdf

                document = pymupdf.open(str(source_item.path))
                for page_number in page_numbers:
                    check_cancel(cancel_event)
                    completed = summary.pages_attempted
                    _emit(
                        progress_callback,
                        file_name=source_item.relative_path,
                        page_number=page_number,
                        completed=completed,
                        total=summary.pages_considered,
                        substage="inspecting_pdf",
                    )
                    try:
                        page = document[page_number - 1]
                        _emit(
                            progress_callback,
                            file_name=source_item.relative_path,
                            page_number=page_number,
                            completed=completed,
                            total=summary.pages_considered,
                            substage="rendering_page",
                        )
                        render_started = time.perf_counter_ns()
                        image_bytes, render_metadata = _render_page(page, pymupdf)
                        result.timings.image_read_ms += (time.perf_counter_ns() - render_started) / 1_000_000
                        image_identity = _image_identity(source_item, image_bytes, "image/png")
                        request = VisionRequest(
                            image_bytes=image_bytes,
                            media_type="image/png",
                            model=provider_model,
                            output_contract=VISION_OUTPUT_CONTRACT,
                        )
                        _emit(
                            progress_callback,
                            file_name=source_item.relative_path,
                            page_number=page_number,
                            completed=completed,
                            total=summary.pages_considered,
                            substage="calling_model",
                        )
                        provider_started = time.perf_counter_ns()
                        summary.provider_calls += 1
                        response = provider.extract(request)
                        result.timings.provider_ms += (time.perf_counter_ns() - provider_started) / 1_000_000
                        result.warnings.append(
                            {
                                "page_number": page_number,
                                "provider": _provider_value(provider, "name", "vision-provider"),
                                "provider_contract": provider_contract,
                                "model": provider_model,
                                "provider_request_id": response.request_id,
                                "response_bytes": response.raw_size_bytes,
                                "render_metadata": render_metadata,
                                "profile_route": profile.get("classification"),
                            }
                        )
                        check_cancel(cancel_event)
                        _emit(
                            progress_callback,
                            file_name=source_item.relative_path,
                            page_number=page_number,
                            completed=completed,
                            total=summary.pages_considered,
                            substage="validating_response",
                        )
                        validation_started = time.perf_counter_ns()
                        page_document = validate_vision_payload(response.payload)
                        result.timings.validation_ms += (time.perf_counter_ns() - validation_started) / 1_000_000
                        page_box = _page_bbox(page)
                        if not page_document.is_empty:
                            for table_index, table in enumerate(page_document.tables):
                                _write_table_asset(
                                    source=source_item,
                                    result=result,
                                    document=page_document,
                                    table=table,
                                    table_index=table_index,
                                    image_identity=image_identity,
                                    provider_name=_provider_value(provider, "name", "vision-provider"),
                                    provider_model=provider_model,
                                    provider_contract=provider_contract,
                                    source_kind=SourceKind.PAGE,
                                    page_number=page_number,
                                    bbox=page_box,
                                    source_locator=f"pdf:page:{page_number}:vision:table:{table_index}:contract:{VISION_CONTRACT_VERSION}",
                                    asset_index=page_number * 10_000 + table_index,
                                    render_metadata=render_metadata,
                                    pipeline_version=paths.VISION_PDF_PIPELINE_VERSION,
                                    configuration_version=paths.VISION_PDF_CONFIG_VERSION,
                                )
                            _write_text_asset(
                                source=source_item,
                                result=result,
                                document=page_document,
                                image_identity=image_identity,
                                provider_name=_provider_value(provider, "name", "vision-provider"),
                                provider_model=provider_model,
                                provider_contract=provider_contract,
                                source_kind=SourceKind.PAGE,
                                page_number=page_number,
                                bbox=page_box,
                                source_locator=f"pdf:page:{page_number}:vision:text:contract:{VISION_CONTRACT_VERSION}",
                                asset_index=page_number - 1,
                                section=f"page-{page_number}-vision",
                                render_metadata=render_metadata,
                                pipeline_version=paths.VISION_PDF_PIPELINE_VERSION,
                                configuration_version=paths.VISION_PDF_CONFIG_VERSION,
                            )
                        result.pages_succeeded += 1
                        summary.pages_succeeded += 1
                    except CancellationRequested:
                        raise
                    except (VisionContractError, VisionProviderError, VisionExtractionError) as exc:
                        result.pages_failed += 1
                        summary.pages_failed += 1
                        result.status = "partial"
                        category = "vision_contract_error" if isinstance(exc, VisionContractError) else "vision_page_failure"
                        result.issues.append(_page_issue(source_item, page_number, category, _safe_error(provider, exc)))
                        result.warnings.append(
                            {
                                "page_number": page_number,
                                "failure": category,
                                "error": _safe_error(provider, exc),
                            }
                        )
                    except Exception as exc:  # one page must not poison the PDF
                        result.pages_failed += 1
                        summary.pages_failed += 1
                        result.status = "partial"
                        result.issues.append(
                            _page_issue(source_item, page_number, "vision_page_failure", _safe_error(provider, exc))
                        )
                    result.status = "partial" if result.pages_failed else "successful"
                    _emit(
                        progress_callback,
                        file_name=source_item.relative_path,
                        page_number=page_number,
                        completed=summary.pages_attempted + 1,
                        total=summary.pages_considered,
                        substage="writing_assets",
                    )
                    _record_page(registry, result, summary, started_at, force)
                    summary.pages_attempted += 1
                if document.page_count < max(page_numbers):
                    raise VisionExtractionError("PDF page count changed during Vision extraction")
                after_bytes = source_item.path.read_bytes()
                if hashlib.sha256(after_bytes).hexdigest() != source_item.content_sha256:
                    raise VisionExtractionError("source PDF content hash changed during Vision extraction")
                summary.extracted += 1
            except CancellationRequested:
                registry.recover_incomplete_extractions(source_root)
                raise
            except Exception as exc:
                result.status = "failed"
                result.error_category = "vision_pdf_extraction_error"
                result.error_message = _safe_error(provider, exc)
                result.text_assets.clear()
                result.text_chunks.clear()
                result.table_assets.clear()
                # A page-level partial was already committed, so retain it if
                # the late failure is only a source-integrity check.
                _record_page(registry, result, summary, started_at, force)
                summary.failed += 1
            finally:
                if document is not None:
                    document.close()
            summary.text_assets_produced += len(result.text_assets)
            summary.table_assets_produced += len(result.table_assets)
            summary.text_chars += result.total_chars
            summary.table_rows += result.total_rows
            summary.quality_issues += len(result.issues)
    finally:
        registry.close()
    # Counts are read from the catalog by the unified coordinator.  Standalone
    # callers still receive useful page/call/error metrics above.
    summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    return summary
