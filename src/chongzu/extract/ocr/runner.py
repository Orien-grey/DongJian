"""Registry-backed offline OCR runner for images and scanned PDF pages."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from chongzu import paths
from chongzu.assets import (
    BoundingBox,
    ChunkProvenance,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueStatus,
    SourceKind,
    TableAsset,
    TextAsset,
    TextChunk,
    make_chunk_id,
    make_text_asset_id,
    utc_now,
)
from chongzu.registry import Registry, canonical_source_root, utc_now as registry_now
from chongzu.scan import scan_source

from ..models import StructuredSource
from ..pdf.artifacts import write_text_asset
from ..pdf.blocks import chunk_text, normalize_text
from ..pdf.table_quality import DetectedTable
from .img2table_adapter import (
    IMAGE_TABLE_EXTRACTOR,
    IMAGE_TABLE_EXTRACTOR_VERSION,
    ImageTableAdapterError,
    ImageTableConfig,
    extract_tables_from_document,
    load_image_document,
    publish_image_table,
)
from .rapidocr_engine import OCRBlock, OCREngineError, RapidOCREngine


MAX_OCR_WORKERS = 2


class OCRExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRTarget:
    kind: str  # image or pdf_page
    page_number: int | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "page_number": self.page_number, "reason": self.reason}


@dataclass
class OCRStageTimings:
    render_ms: float = 0.0
    ocr_ms: float = 0.0
    image_table_ms: float = 0.0
    artifact_write_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "render_ms": self.render_ms,
            "ocr_ms": self.ocr_ms,
            "image_table_ms": self.image_table_ms,
            "artifact_write_ms": self.artifact_write_ms,
        }


@dataclass
class OCRExtractionResult:
    source: StructuredSource
    extraction_run_id: str
    extraction_identity: str
    route_reason: str
    ocr_targets: list[dict[str, Any]] = field(default_factory=list)
    status: str = "successful"
    text_assets: list[TextAsset] = field(default_factory=list)
    text_chunks: list[TextChunk] = field(default_factory=list)
    table_assets: list[TableAsset] = field(default_factory=list)
    detected_tables: list[DetectedTable] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    timings: OCRStageTimings = field(default_factory=OCRStageTimings)
    image_table_extraction_failed: bool = False
    error_category: str | None = None
    error_message: str | None = None

    @property
    def extractor(self) -> str:
        return RapidOCREngine.extractor

    @property
    def extractor_version(self) -> str:
        return RapidOCREngine.extractor_version

    @property
    def target_count(self) -> int:
        return len(self.ocr_targets)

    @property
    def total_chars(self) -> int:
        return sum(len(asset.text) for asset in self.text_assets)

    @property
    def total_rows(self) -> int:
        return sum(asset.row_count for asset in self.table_assets)


@dataclass
class OCRExtractionSummary:
    source_root: str
    files_considered: int = 0
    images_considered: int = 0
    pdfs_considered: int = 0
    files_attempted: int = 0
    extracted: int = 0
    reused: int = 0
    deferred_to_profile: int = 0
    targets: int = 0
    pages_ocred: int = 0
    text_assets_produced: int = 0
    table_assets_produced: int = 0
    image_table_extraction_failures: int = 0
    image_table_ocr_calls: int = 0
    image_table_ms: float = 0.0
    ocr_chars: int = 0
    failures: int = 0
    quality_issues: int = 0
    render_ms: float = 0.0
    ocr_ms: float = 0.0
    artifact_write_ms: float = 0.0
    registry_write_ms: float = 0.0
    discovery_scan_ms: float = 0.0
    wall_time_ms: float = 0.0

    def benchmark_metrics(self) -> dict[str, float | int | str]:
        seconds = self.wall_time_ms / 1000.0 if self.wall_time_ms else 0.0
        return {
            "files": self.files_attempted,
            "images": self.images_considered,
            "PDFs": self.pdfs_considered,
            "reused": self.reused,
            "targets": self.targets,
            "pages OCRed": self.pages_ocred,
            "OCR chars": self.ocr_chars,
            "table assets": self.table_assets_produced,
            "image table extraction failures": self.image_table_extraction_failures,
            "RapidOCR calls": self.image_table_ocr_calls,
            "image table ms": self.image_table_ms,
            "files/sec": self.files_attempted / seconds if seconds else 0.0,
            "pages/sec": self.pages_ocred / seconds if seconds else 0.0,
            "render ms": self.render_ms,
            "OCR ms": self.ocr_ms,
            "artifact write ms": self.artifact_write_ms,
            "registry write ms": self.registry_write_ms,
            "wall clock ms": self.wall_time_ms,
            "peak memory": "not collected (no zero-cost reliable cross-process metric)",
        }


def normalize_ocr_workers(workers: int | None) -> int:
    value = 1 if workers is None else int(workers)
    if value < 1 or value > MAX_OCR_WORKERS:
        raise ValueError(f"OCR workers must be between 1 and {MAX_OCR_WORKERS}")
    return value


def _source_from_row(row: dict[str, Any], workspace_root: Path) -> StructuredSource:
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


def ocr_extraction_identity(source: StructuredSource, targets: list[OCRTarget]) -> str:
    payload = {
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "business_format": source.business_format,
        "extractor": RapidOCREngine.extractor,
        "extractor_version": RapidOCREngine.extractor_version,
        "config_version": paths.OCR_CONFIG_VERSION,
        "pipeline_version": paths.OCR_PIPELINE_VERSION,
        "targets": [target.as_dict() for target in targets],
        "registry_schema_version": paths.REGISTRY_SCHEMA_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _profile_targets(profile: dict[str, Any] | None) -> tuple[list[OCRTarget], str]:
    if not profile:
        return [], "phase4a_profile_missing_defer"
    classification = str(profile.get("classification") or "unknown")
    pages = profile.get("pages")
    if not isinstance(pages, list):
        return [], "phase4a_profile_invalid_defer"
    targets: list[OCRTarget] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        number = int(page.get("page_number") or 0)
        if number < 1:
            continue
        # OCR only pages without reliable native text.  A page may carry both
        # flags in a mixed PDF; native text remains authoritative when present.
        if not bool(page.get("native_text_available")) and (
            bool(page.get("suspected_scanned")) or classification in {"mixed", "suspected_scanned", "unknown"}
        ):
            targets.append(
                OCRTarget(
                    kind="pdf_page",
                    page_number=number,
                    reason="phase4a_suspected_scanned_page"
                    if bool(page.get("suspected_scanned"))
                    else "phase4a_mixed_page_without_native_text",
                )
            )
    if targets:
        return targets, "phase4a_profile_selected_scanned_pages"
    if classification == "suspected_scanned":
        return [], "phase4a_scanned_profile_without_selectable_pages"
    return [], "phase4a_native_text_pages_only"


def _bbox_from_block(block: OCRBlock, *, scale_x: float = 1.0, scale_y: float = 1.0) -> BoundingBox | None:
    if len(block.bbox) < 4:
        return None
    points = [(point[0] * scale_x, point[1] * scale_y) for point in block.bbox]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    try:
        return BoundingBox(min(xs), min(ys), max(xs), max(ys))
    except ValueError:
        return None


def _quality_issue(source: StructuredSource, asset_id: str, issue_type: str, evidence: dict[str, Any]) -> QualityIssue:
    identity = json.dumps(
        [source.file_id, source.content_sha256, asset_id, issue_type, evidence],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return QualityIssue(
        issue_id=f"issue_{hashlib.sha256(identity).hexdigest()[:32]}",
        asset_id=asset_id,
        severity=QualityIssueSeverity.WARNING,
        issue_type=issue_type,
        description=issue_type.replace("_", " "),
        evidence=evidence,
        detected_by="rapidocr-local-v1",
        suggested_action="Review OCR evidence before downstream semantic processing.",
        status=QualityIssueStatus.OPEN,
    )


def _publish_asset(
    *,
    result: OCRExtractionResult,
    target: OCRTarget,
    blocks: list[OCRBlock],
    image_width: int,
    image_height: int,
    page_bbox: BoundingBox,
    image_id: str | None = None,
    page_rotation: int | None = None,
    pdf_scale: tuple[float, float] = (1.0, 1.0),
) -> None:
    source = result.source
    page_number = target.page_number
    source_kind = SourceKind.IMAGE if target.kind == "image" else SourceKind.PAGE
    locator = (
        f"image:full:config:{paths.OCR_CONFIG_VERSION}"
        if target.kind == "image"
        else f"page:{page_number}:ocr:config:{paths.OCR_CONFIG_VERSION}"
    )
    asset_id = make_text_asset_id(
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extractor=result.extractor,
        extractor_version=result.extractor_version,
        source_kind=source_kind,
        source_locator=locator,
        asset_index=0 if page_number is None else page_number - 1,
    )
    raw_text = "\n".join(block.text for block in blocks)
    normalized = normalize_text(raw_text)
    average_confidence = (
        sum(block.confidence for block in blocks if block.confidence is not None)
        / max(1, sum(1 for block in blocks if block.confidence is not None))
        if any(block.confidence is not None for block in blocks)
        else None
    )
    block_payload = []
    for block in blocks:
        bbox = _bbox_from_block(block, scale_x=pdf_scale[0], scale_y=pdf_scale[1])
        block_payload.append(
            {
                "text": block.text,
                "confidence": block.confidence,
                "bbox": bbox.__dict__ if bbox else None,
                "bbox_points": [list(point) for point in block.bbox],
                "page_number": block.page_number,
                "image": block.image or image_id,
                "block_index": block.block_index,
                "extractor": block.extractor,
                "extractor_version": block.extractor_version,
                "coordinate_space": "pdf_points" if target.kind == "pdf_page" else "image_pixels",
            }
        )
    metadata = {
        "contract_version": paths.OCR_CONFIG_VERSION,
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "source_relative_path": source.relative_path,
        "extraction_run_id": result.extraction_run_id,
        "extractor": result.extractor,
        "extractor_version": result.extractor_version,
        "source_kind": source_kind.value,
        "page_number": page_number,
        "source_bbox": page_bbox.__dict__,
        "image_width": image_width,
        "image_height": image_height,
        "page_rotation": page_rotation,
        "ocr_target_reason": target.reason,
        "ocr_blocks": block_payload,
        "ocr_block_contract": "OCRBlock-v1",
        "average_confidence": average_confidence,
        "chunk_config_version": paths.TEXT_CHUNK_CONFIG_VERSION,
        "offset_basis": "normalized_text",
    }
    artifact_started = time.perf_counter_ns()
    paths_written = write_text_asset(
        workspace_root=source.workspace_root,
        text_asset_id=asset_id,
        raw_text=raw_text,
        normalized_text=normalized,
        metadata=metadata,
    )
    result.timings.artifact_write_ms += (time.perf_counter_ns() - artifact_started) / 1_000_000
    asset = TextAsset(
        text_asset_id=asset_id,
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extraction_run_id=result.extraction_run_id,
        extractor=result.extractor,
        extractor_version=result.extractor_version,
        source_kind=source_kind,
        page_number=page_number,
        section="image" if page_number is None else f"page-{page_number}-ocr",
        bbox=page_bbox,
        text=normalized,
        language=None,
        created_at=utc_now(),
        source_relative_path=source.relative_path,
        raw_artifact_path=paths_written["raw"],
        normalized_artifact_path=paths_written["normalized"],
        metadata_artifact_path=paths_written["metadata"],
    )
    result.text_assets.append(asset)
    for chunk in chunk_text(normalized):
        result.text_chunks.append(
            TextChunk(
                chunk_id=make_chunk_id(
                    text_asset_id=asset_id,
                    chunk_index=chunk.chunk_index,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    chunk_config_version=paths.TEXT_CHUNK_CONFIG_VERSION,
                ),
                text_asset_id=asset_id,
                file_id=source.file_id,
                chunk_index=chunk.chunk_index,
                text=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                provenance=ChunkProvenance(
                    file_id=source.file_id,
                    content_sha256=source.content_sha256,
                    text_asset_id=asset_id,
                    extraction_run_id=result.extraction_run_id,
                    extractor=result.extractor,
                    extractor_version=result.extractor_version,
                    source_kind=source_kind,
                    page_number=page_number,
                    section=asset.section,
                ),
            )
        )
    if not blocks:
        result.issues.append(_quality_issue(source, asset_id, "ocr_no_text_detected", {"target": target.as_dict()}))
    elif average_confidence is not None and average_confidence < 0.65:
        result.issues.append(
            _quality_issue(
                source,
                asset_id,
                "low_ocr_confidence",
                {"average_confidence": average_confidence, "block_count": len(blocks)},
            )
        )


def _extract_one(source: StructuredSource, run_id: str, identity: str, targets: list[OCRTarget], route_reason: str) -> OCRExtractionResult:
    result = OCRExtractionResult(
        source=source,
        extraction_run_id=run_id,
        extraction_identity=identity,
        route_reason=route_reason,
        ocr_targets=[target.as_dict() for target in targets],
    )
    try:
        before = source.path.stat()
        if (before.st_size, before.st_mtime_ns) != (source.size_bytes, source.mtime_ns):
            raise OCREngineError("source size or mtime changed after registry scan")
        engine = RapidOCREngine()

        def process_target(
            *,
            target: OCRTarget,
            image_document: Any,
            image: Any,
            image_width: int,
            image_height: int,
            page_bbox: BoundingBox,
            page_rotation: int | None = None,
            pdf_scale: tuple[float, float] = (1.0, 1.0),
        ) -> None:
            blocks, ocr_ms = engine.recognize(
                image,
                page_number=target.page_number,
                image_id=source.relative_path,
            )
            result.timings.ocr_ms += ocr_ms
            result.warnings.append(
                {
                    "target": target.as_dict(),
                    "ocr_calls": 1,
                    "ocr_blocks": len(blocks),
                    "table_adapter": IMAGE_TABLE_EXTRACTOR,
                    "table_adapter_version": IMAGE_TABLE_EXTRACTOR_VERSION,
                    "ocr_reused_by_table_adapter": True,
                    "img2table_ocr_backend_calls": 0,
                }
            )
            _publish_asset(
                result=result,
                target=target,
                blocks=blocks,
                image_width=image_width,
                image_height=image_height,
                page_bbox=page_bbox,
                image_id=source.relative_path,
                page_rotation=page_rotation,
                pdf_scale=pdf_scale,
            )
            try:
                tables, _width, _height, table_ms = extract_tables_from_document(
                    image_document,
                    blocks,
                    config=ImageTableConfig(),
                )
            except ImageTableAdapterError as exc:
                result.image_table_extraction_failed = True
                result.status = "partial"
                result.issues.append(
                    _quality_issue(
                        source,
                        f"image-table:{source.file_id}:page:{target.page_number or 1}",
                        "image_table_extractor_error",
                        {
                            "target": target.as_dict(),
                            "error": str(exc),
                            "ocr_reused": True,
                        },
                    )
                )
                return
            result.timings.image_table_ms += table_ms
            for table_index, table in enumerate(tables):
                try:
                    publication = publish_image_table(
                        source=source,
                        extraction_run_id=result.extraction_run_id,
                        table=table,
                        table_index=table_index,
                        blocks=blocks,
                        image_width=image_width,
                        image_height=image_height,
                        source_kind=SourceKind.IMAGE if target.kind == "image" else SourceKind.PAGE,
                        page_number=target.page_number,
                        scale_x=pdf_scale[0],
                        scale_y=pdf_scale[1],
                        config=ImageTableConfig(),
                    )
                except Exception as exc:
                    # OCR text is already a valid independent result.  A
                    # table artifact/write failure must therefore remain
                    # partial instead of discarding the text asset.
                    result.image_table_extraction_failed = True
                    result.status = "partial"
                    result.issues.append(
                        _quality_issue(
                            source,
                            f"image-table:{source.file_id}:page:{target.page_number or 1}:table:{table_index}",
                            "image_table_extractor_error",
                            {
                                "target": target.as_dict(),
                                "table_index": table_index,
                                "error": str(exc),
                                "ocr_reused": True,
                            },
                        )
                    )
                    continue
                result.timings.artifact_write_ms += publication.artifact_write_ms
                if publication.asset is not None:
                    result.table_assets.append(publication.asset)
                if publication.detected is not None:
                    result.detected_tables.append(publication.detected)
                result.issues.extend(publication.issues)
            result.warnings.append(
                {
                    "target": target.as_dict(),
                    "image_table_count": len(tables),
                    "image_table_ocr_reused": True,
                    "image_table_ocr_backend_calls": 0,
                    "image_table_ms": table_ms,
                }
            )

        if source.business_format in {"jpeg", "png"}:
            target = targets[0]
            image_document = load_image_document(source.path)
            image = image_document.images[0]
            image_height, image_width = int(image.shape[0]), int(image.shape[1])
            process_target(
                target=target,
                image_document=image_document,
                image=image,
                image_width=image_width,
                image_height=image_height,
                page_bbox=BoundingBox(0.0, 0.0, float(image_width), float(image_height)),
            )
        else:
            import pymupdf  # type: ignore[import-not-found]

            with pymupdf.open(str(source.path)) as document:
                for target in targets:
                    page = document[target.page_number - 1]
                    render_started = time.perf_counter_ns()
                    # Use one 200 DPI render for both RapidOCR and img2table;
                    # the table adapter receives the same decoded image and
                    # injected OCRData, so it never starts another OCR pass.
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72), alpha=False)
                    result.timings.render_ms += (time.perf_counter_ns() - render_started) / 1_000_000
                    image_document = load_image_document(pixmap.tobytes("png"))
                    image = image_document.images[0]
                    image_height, image_width = int(image.shape[0]), int(image.shape[1])
                    scale_x = float(page.rect.width) / max(1, image_width)
                    scale_y = float(page.rect.height) / max(1, image_height)
                    process_target(
                        target=target,
                        image_document=image_document,
                        image=image,
                        image_width=image_width,
                        image_height=image_height,
                        page_bbox=BoundingBox(float(page.rect.x0), float(page.rect.y0), float(page.rect.x1), float(page.rect.y1)),
                        page_rotation=int(page.rotation or 0),
                        pdf_scale=(scale_x, scale_y),
                    )
        after = source.path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise OCREngineError("source size or mtime changed during OCR")
    except Exception as exc:  # isolate one image/PDF
        result.status = "failed"
        result.text_assets.clear()
        result.text_chunks.clear()
        result.table_assets.clear()
        result.detected_tables.clear()
        result.issues.clear()
        result.error_category = "ocr_error"
        result.error_message = str(exc)
    return result


def _artifacts_exist(reusable: dict[str, Any], workspace: Path) -> bool:
    artifacts = reusable.get("artifacts") or []
    if not artifacts:
        return False
    try:
        return all((workspace / str(path)).resolve().is_file() for path in artifacts)
    except OSError:
        return False


def _record(registry: Registry, summary: OCRExtractionSummary, result: OCRExtractionResult, started_at: datetime, force: bool) -> None:
    started = time.perf_counter_ns()
    registry.record_ocr_result(result, started_at=started_at, finished_at=registry_now(), force=force)
    summary.registry_write_ms += (time.perf_counter_ns() - started) / 1_000_000
    summary.render_ms += result.timings.render_ms
    summary.ocr_ms += result.timings.ocr_ms
    summary.image_table_ms += result.timings.image_table_ms
    summary.artifact_write_ms += result.timings.artifact_write_ms
    summary.quality_issues += len(result.issues)
    summary.table_assets_produced += len(result.table_assets)
    summary.image_table_extraction_failures += int(result.image_table_extraction_failed)
    summary.image_table_ocr_calls += len(result.ocr_targets)
    if result.status == "failed":
        summary.failures += 1
        return
    summary.extracted += 1
    summary.pages_ocred += sum(1 for target in result.ocr_targets if target.get("kind") == "pdf_page")
    summary.text_assets_produced += len(result.text_assets)
    summary.ocr_chars += result.total_chars


def extract_ocr(
    source: Path | str,
    *,
    workers: int | None = None,
    force: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    _scan_summary: Any | None = None,
    selected_relative_paths: set[str] | None = None,
) -> OCRExtractionSummary:
    """Run offline OCR for images and only scanned PDF pages in *source*.

    ``selected_relative_paths`` is a deliberately narrow benchmark hook.  It
    filters already-registered image/PDF candidates after the normal scan and
    is used by the rendered-page consistency benchmark; it does not change
    production routing or the persisted source registry facts.
    """

    wall_started = time.perf_counter_ns()
    worker_count = normalize_ocr_workers(workers)
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if _scan_summary is None:
        try:
            scan_summary = scan_source(source, workers=worker_count, registry_path=registry_file)
        except Exception as exc:
            raise OCRExtractionError(str(exc)) from exc
    else:
        scan_summary = _scan_summary
    source_root = canonical_source_root(source, require_directory=True)
    summary = OCRExtractionSummary(source_root=source_root, discovery_scan_ms=scan_summary.elapsed_ms)
    registry = Registry.open(registry_file)
    try:
        # Profiles are the sole PDF routing facts.  Establish them through the
        # existing Phase 4A route, which reuses unchanged native results.
        if any(row.get("business_format") == "pdf" for row in registry.ocr_candidates(source_root)):
            from ..pdf.runner import extract_pdf

            extract_pdf(
                source,
                workers=worker_count,
                force=False,
                registry_path=registry_file,
                workspace_root=workspace,
                _scan_summary=scan_summary,
            )
        registry.recover_incomplete_extractions(source_root)
        rows = registry.ocr_candidates(source_root)
        if selected_relative_paths is not None:
            selected = {str(path).replace("\\", "/") for path in selected_relative_paths}
            rows = [
                row
                for row in rows
                if str(row.get("relative_path") or "").replace("\\", "/") in selected
            ]
        summary.files_considered = len(rows)
        summary.images_considered = sum(1 for row in rows if row.get("business_format") in {"jpeg", "png"})
        summary.pdfs_considered = sum(1 for row in rows if row.get("business_format") == "pdf")
        pending: list[tuple[StructuredSource, list[OCRTarget], str]] = []
        for row in rows:
            source_item = _source_from_row(row, workspace)
            if source_item.business_format in {"jpeg", "png"}:
                pending.append((source_item, [OCRTarget("image", None, "image_candidate")], "image_candidate"))
                continue
            profile_record = registry.current_pdf_profile(source_item.file_id, source_item.content_sha256)
            profile = profile_record.get("profile") if profile_record else None
            targets, route_reason = _profile_targets(profile)
            if not targets:
                if route_reason.startswith("phase4a_profile_missing") or route_reason.startswith("phase4a_profile_invalid"):
                    summary.deferred_to_profile += 1
                continue
            pending.append((source_item, targets, route_reason))
        summary.files_attempted = len(pending)

        def submit(executor: ProcessPoolExecutor, item: tuple[StructuredSource, list[OCRTarget], str]):
            source_item, targets, route_reason = item
            identity = ocr_extraction_identity(source_item, targets)
            reusable = None if force else registry.reusable_ocr_extraction(identity)
            if reusable is not None and _artifacts_exist(reusable, workspace):
                summary.reused += 1
                summary.targets += len(targets)
                summary.pages_ocred += sum(1 for target in targets if target.kind == "pdf_page")
                summary.text_assets_produced += int(reusable.get("text_asset_count") or len(reusable.get("text_assets") or []))
                summary.table_assets_produced += int(reusable.get("table_count") or 0)
                return None
            run_id = f"xrun_{uuid4().hex}"
            started_at = registry_now()
            registry.start_ocr_extraction(
                extraction_run_id=run_id,
                extraction_identity=identity,
                source=source_item,
                extractor=RapidOCREngine.extractor,
                extractor_version=RapidOCREngine.extractor_version,
                started_at=started_at,
                force=force,
                route_reason=route_reason,
            )
            summary.targets += len(targets)
            if executor is None:
                return _extract_one(source_item, run_id, identity, targets, route_reason), started_at, run_id, identity
            return executor.submit(_extract_one, source_item, run_id, identity, targets, route_reason), started_at, run_id, identity

        if worker_count == 1:
            for item in pending:
                submitted = submit(None, item)  # type: ignore[arg-type]
                if submitted is None:
                    continue
                result, started_at, _run_id, _identity = submitted
                _record(registry, summary, result, started_at, force)
        else:
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                pending_futures: dict[
                    Future[OCRExtractionResult],
                    tuple[datetime, StructuredSource, str, list[OCRTarget], str, str],
                ] = {}
                for item in pending:
                    submitted = submit(executor, item)
                    if submitted is not None:
                        future, started_at, run_id, identity = submitted
                        pending_futures[future] = (started_at, item[0], item[2], item[1], run_id, identity)
                while pending_futures:
                    done, _ = wait(tuple(pending_futures), return_when=FIRST_COMPLETED)
                    for future in done:
                        started_at, source_item, route_reason, targets, run_id, identity = pending_futures.pop(future)
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = OCRExtractionResult(
                                source=source_item,
                                extraction_run_id=run_id,
                                extraction_identity=identity,
                                route_reason=route_reason,
                                ocr_targets=[target.as_dict() for target in targets],
                                status="failed",
                                error_category="worker_error",
                                error_message=str(exc),
                            )
                        _record(registry, summary, result, started_at, force)
    finally:
        registry.close()
    summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    return summary
