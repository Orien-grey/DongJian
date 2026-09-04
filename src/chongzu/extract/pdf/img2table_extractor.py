"""Native-text PDF table candidate adapter for img2table.

The candidate is deliberately isolated from the Phase 4A PyMuPDF text route.
It receives page indexes selected from the stored Phase 4A profile, disables
OCR explicitly, and publishes the common ``TableAsset`` artifact contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
import hashlib
import importlib
import json
from pathlib import Path
import statistics
import time
from typing import Any, Sequence
import unicodedata

from chongzu import paths
from chongzu.assets import (
    AssetQualityStatus,
    BoundingBox,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueStatus,
    SourceKind,
    TableAsset,
    make_table_id,
    utc_now,
)

from ..artifacts import (
    matrix_frame,
    positional_names,
    workspace_relative,
    write_json_atomic,
    write_parquet_atomic,
)
from ..img2table_compat import ensure_img2table_threshold_compat
from ..models import StructuredSource
from .profiling import TABLE_CANDIDATE_SEMANTICS
from .table_quality import DetectedTable


EXTRACTOR_NAME = "img2table-candidate"
try:
    EXTRACTOR_VERSION = version("img2table")
except PackageNotFoundError:
    EXTRACTOR_VERSION = paths.IMG2TABLE_VERSION


@dataclass(frozen=True)
class PDFTableConfig:
    """Deterministic candidate settings; no OCR engine is constructed."""

    borderless_tables: bool = True
    implicit_rows: bool = False
    implicit_columns: bool = False
    min_confidence: int = 50
    detect_rotation: bool = False
    max_workers: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "borderless_tables": self.borderless_tables,
            "implicit_rows": self.implicit_rows,
            "implicit_columns": self.implicit_columns,
            "min_confidence": self.min_confidence,
            "detect_rotation": self.detect_rotation,
            "max_workers": self.max_workers,
            "ocr_enabled": False,
            "pdf_text_extraction": True,
            "config_version": paths.PDF_TABLE_CONFIG_VERSION,
        }


@dataclass
class PDFTableStageTimings:
    extraction_ms: float = 0.0
    normalization_ms: float = 0.0
    parquet_write_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "extraction_ms": self.extraction_ms,
            "normalization_ms": self.normalization_ms,
            "parquet_write_ms": self.parquet_write_ms,
        }


@dataclass
class PDFTableExtractionResult:
    source: StructuredSource
    extraction_run_id: str
    extraction_identity: str
    route_reason: str
    profile_classification: str
    pages_attempted: int = 0
    pages_deferred: int = 0
    extractor: str = EXTRACTOR_NAME
    extractor_version: str = EXTRACTOR_VERSION
    status: str = "successful"
    assets: list[TableAsset] = field(default_factory=list)
    detected_tables: list[DetectedTable] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    ground_truth_summary: dict[str, int] | None = None
    timings: PDFTableStageTimings = field(default_factory=PDFTableStageTimings)
    error_category: str | None = None
    error_message: str | None = None

    @property
    def total_rows(self) -> int:
        return sum(asset.row_count for asset in self.assets)

    @property
    def total_cells(self) -> int:
        return sum(asset.row_count * asset.column_count for asset in self.assets)


def _require_img2table():
    try:
        module = importlib.import_module("img2table.document")
        return module.PDF
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "img2table candidate is not provisioned in runtime\\packages; "
            "run the project-local bootstrap before PDF table extraction"
        ) from exc


def _normal_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value).strip()
    return value


def _normalize_matrix(rows: Sequence[Sequence[Any]], width: int) -> list[list[Any]]:
    return [
        [_normal_cell(value) for value in list(row[:width]) + [None] * max(0, width - len(row))]
        for row in rows
    ]


def _table_bbox(table: Any, page: Any) -> tuple[BoundingBox | None, list[str]]:
    bbox = getattr(table, "bbox", None)
    image_width = getattr(bbox, "image_width", None)
    image_height = getattr(bbox, "image_height", None)
    if bbox is None or not image_width or not image_height:
        return None, ["bbox_unavailable"]
    # img2table reports top-left pixel coordinates for its 200 DPI render.
    # PyMuPDF page rectangles use top-left PDF points, so scale explicitly.
    if int(getattr(page, "rotation", 0) or 0) % 360:
        return None, ["bbox_unavailable_for_rotated_page"]
    try:
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        converted = BoundingBox(
            float(bbox.x1) / float(image_width) * page_width,
            float(bbox.y1) / float(image_height) * page_height,
            float(bbox.x2) / float(image_width) * page_width,
            float(bbox.y2) / float(image_height) * page_height,
        )
    except (AttributeError, TypeError, ValueError):
        return None, ["bbox_unavailable"]
    return converted, []


def _merged_cell_evidence(table: Any) -> bool:
    cells = [cell for row in getattr(table, "content", {}).values() for cell in row]
    widths = [float(cell.bbox.x2 - cell.bbox.x1) for cell in cells if getattr(cell, "bbox", None)]
    if len(widths) < 4:
        return False
    median_width = statistics.median(widths)
    if median_width <= 0:
        return False
    return any(width > median_width * 1.6 for width in widths)


def _issue(
    *,
    source: StructuredSource,
    asset_id: str,
    issue_type: str,
    evidence: dict[str, Any],
    severity: QualityIssueSeverity = QualityIssueSeverity.WARNING,
) -> QualityIssue:
    identity = json.dumps(
        [source.file_id, source.content_sha256, asset_id, issue_type, evidence],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    action = {
        "suspicious_single_column": "Review whether the candidate is a real table or a text list.",
        "suspicious_single_row": "Review whether a header or row boundary was lost.",
        "possible_merged_cells": "Compare cell spans with the source PDF before accepting values.",
        "possible_column_shift": "Review ragged rows and cell alignment against the source page.",
        "multiple_tables_on_page": "Review each table boundary independently.",
        "empty_detected_table": "Verify the candidate region and table detector output.",
        "deferred_to_ocr": "Queue the page for the Phase 5B OCR/image route; do not treat this as extraction failure.",
        "table_extractor_error": "Review the candidate dependency/runtime error and preserve the source PDF.",
    }.get(issue_type, "Review the candidate table against the source PDF.")
    return QualityIssue(
        issue_id=f"issue_{hashlib.sha256(identity).hexdigest()[:32]}",
        asset_id=asset_id,
        severity=severity,
        issue_type=issue_type,
        description=issue_type.replace("_", " "),
        evidence=evidence,
        detected_by=f"{EXTRACTOR_NAME}:{EXTRACTOR_VERSION}",
        suggested_action=action,
        status=QualityIssueStatus.OPEN,
    )


def _write_table(
    *,
    source: StructuredSource,
    result: PDFTableExtractionResult,
    page_number: int,
    table_index: int,
    table: Any,
    page: Any,
    config: PDFTableConfig,
) -> None:
    raw_rows = [
        [getattr(cell, "value", None) for cell in row]
        for row in getattr(table, "content", {}).values()
    ]
    width = max((len(row) for row in raw_rows), default=0)
    if not raw_rows or width == 0:
        candidate_id = f"pdf-table:{source.file_id}:page:{page_number}:table:{table_index}"
        result.issues.append(
            _issue(
                source=source,
                asset_id=candidate_id,
                issue_type="empty_detected_table",
                evidence={"page_number": page_number, "table_index": table_index},
            )
        )
        return

    config_digest = hashlib.sha256(
        json.dumps(config.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    source_locator = (
        f"pdf:page:{page_number}:table:{table_index}:config:{paths.PDF_TABLE_CONFIG_VERSION}:{config_digest}"
    )
    table_id = make_table_id(
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extractor=result.extractor,
        extractor_version=result.extractor_version,
        source_kind=SourceKind.PAGE,
        source_locator=source_locator,
        asset_index=table_index,
    )
    target_dir = source.workspace_root / "artifacts" / "tables" / table_id
    raw_path = target_dir / "raw.parquet"
    normalized_path = target_dir / "normalized.parquet"
    metadata_path = target_dir / "metadata.json"
    raw_names = positional_names(width)
    normalized_names = [f"column_{index + 1:04d}" for index in range(width)]
    normalization_started = time.perf_counter_ns()
    normalized_rows = _normalize_matrix(raw_rows, width)
    result.timings.normalization_ms += (time.perf_counter_ns() - normalization_started) / 1_000_000

    result.timings.parquet_write_ms += write_parquet_atomic(
        matrix_frame(raw_rows, raw_names), raw_path
    )
    result.timings.parquet_write_ms += write_parquet_atomic(
        matrix_frame(normalized_rows, normalized_names), normalized_path
    )

    bbox, bbox_limitations = _table_bbox(table, page)
    widths = [len(row) for row in raw_rows]
    quality_types: list[str] = []
    if width == 1 and len(raw_rows) >= 2:
        quality_types.append("suspicious_single_column")
    if len(raw_rows) == 1 and width >= 2:
        quality_types.append("suspicious_single_row")
    if len(set(widths)) > 1:
        quality_types.append("possible_column_shift")
    if _merged_cell_evidence(table):
        quality_types.append("possible_merged_cells")

    metadata = {
        "contract_version": paths.PDF_TABLE_CONFIG_VERSION,
        "table_id": table_id,
        "candidate_status": "candidate",
        "candidate_semantics": TABLE_CANDIDATE_SEMANTICS,
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "source_relative_path": source.relative_path,
        "source_kind": SourceKind.PAGE.value,
        "page_number": page_number,
        "table_index": table_index,
        "source_coordinate_system": "PDF points, top-left origin",
        "source_range": {
            "row_start": 0,
            "row_end": len(raw_rows),
            "column_start": 0,
            "column_end": width,
            "coordinate_system": "zero-based half-open candidate matrix",
        },
        "candidate_bbox": (
            {
                "x0": bbox.x0,
                "y0": bbox.y0,
                "x1": bbox.x1,
                "y1": bbox.y1,
            }
            if bbox
            else None
        ),
        "img2table_bbox_pixels": (
            {
                "x1": int(table.bbox.x1),
                "y1": int(table.bbox.y1),
                "x2": int(table.bbox.x2),
                "y2": int(table.bbox.y2),
                "image_width": int(table.bbox.image_width),
                "image_height": int(table.bbox.image_height),
            }
            if getattr(table, "bbox", None) is not None
            else None
        ),
        "rows": len(raw_rows),
        "columns": width,
        "raw_row_widths": widths,
        "table_title_candidate": getattr(table, "title", None),
        "extractor": result.extractor,
        "extractor_version": result.extractor_version,
        "extraction_run_id": result.extraction_run_id,
        "ocr_enabled": False,
        "pdf_text_extraction": True,
        "quality_warnings": quality_types,
        "limitations": bbox_limitations,
        "layers": {"raw": "raw.parquet", "normalized": "normalized.parquet", "semantic": None},
        "column_mapping": [
            {
                "source_column_offset": index,
                "raw": raw_names[index],
                "normalized": normalized_names[index],
            }
            for index in range(width)
        ],
    }
    write_json_atomic(metadata_path, metadata)

    asset = TableAsset(
        table_id=table_id,
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extraction_run_id=result.extraction_run_id,
        extractor=result.extractor,
        extractor_version=result.extractor_version,
        source_kind=SourceKind.PAGE,
        source_relative_path=source.relative_path,
        sheet_name=None,
        page_number=page_number,
        bbox=bbox,
        source_row_start=0,
        source_row_end=len(raw_rows),
        source_column_start=0,
        source_column_end=width,
        row_count=len(raw_rows),
        column_count=width,
        columns=tuple(normalized_names),
        raw_artifact_path=workspace_relative(raw_path, source.workspace_root),
        normalized_artifact_path=workspace_relative(normalized_path, source.workspace_root),
        metadata_artifact_path=workspace_relative(metadata_path, source.workspace_root),
        extraction_confidence=None,
        quality_status=AssetQualityStatus.REVIEW if quality_types else AssetQualityStatus.PASS,
        created_at=utc_now(),
    )
    result.assets.append(asset)
    result.detected_tables.append(
        DetectedTable(
            page_number=page_number,
            table_index=table_index,
            rows=tuple(tuple(value for value in row) for row in raw_rows),
            table_id=table_id,
            bbox=(bbox.x0, bbox.y0, bbox.x1, bbox.y1) if bbox else None,
        )
    )
    for issue_type in quality_types:
        result.issues.append(
            _issue(
                source=source,
                asset_id=table_id,
                issue_type=issue_type,
                evidence={
                    "page_number": page_number,
                    "table_index": table_index,
                    "row_widths": widths,
                    "rows": len(raw_rows),
                    "columns": width,
                },
            )
        )


def extract_pdf_tables_file(
    source: StructuredSource,
    extraction_run_id: str,
    extraction_identity: str,
    *,
    page_indexes: Sequence[int],
    profile_classification: str,
    route_reason: str,
    config: PDFTableConfig | None = None,
) -> PDFTableExtractionResult:
    """Extract native-text table candidates from selected zero-based pages."""

    candidate_config = config or PDFTableConfig()
    result = PDFTableExtractionResult(
        source=source,
        extraction_run_id=extraction_run_id,
        extraction_identity=extraction_identity,
        route_reason=route_reason,
        profile_classification=profile_classification,
        pages_attempted=len(page_indexes),
    )
    started = time.perf_counter_ns()
    try:
        PDF = _require_img2table()
        import pymupdf

        ensure_img2table_threshold_compat()
        result.extractor_version = EXTRACTOR_VERSION
        document = PDF(
            source.path,
            pages=list(page_indexes),
            detect_rotation=candidate_config.detect_rotation,
            pdf_text_extraction=True,
        )
        extraction_started = time.perf_counter_ns()
        extracted = document.extract_tables(
            ocr=None,
            implicit_rows=candidate_config.implicit_rows,
            implicit_columns=candidate_config.implicit_columns,
            borderless_tables=candidate_config.borderless_tables,
            min_confidence=candidate_config.min_confidence,
            max_workers=candidate_config.max_workers,
        )
        result.timings.extraction_ms = (time.perf_counter_ns() - extraction_started) / 1_000_000
        pdf_document = pymupdf.open(str(source.path))
        try:
            for zero_based_page, tables in sorted(extracted.items()):
                page_number = int(zero_based_page) + 1
                page = pdf_document[zero_based_page]
                page_assets: list[TableAsset] = []
                for table_index, table in enumerate(tables):
                    before_count = len(result.assets)
                    _write_table(
                        source=source,
                        result=result,
                        page_number=page_number,
                        table_index=table_index,
                        table=table,
                        page=page,
                        config=candidate_config,
                    )
                    if len(result.assets) > before_count:
                        page_assets.extend(result.assets[before_count:])
                if len(tables) > 1:
                    for asset in page_assets:
                        result.issues.append(
                            _issue(
                                source=source,
                                asset_id=asset.table_id,
                                issue_type="multiple_tables_on_page",
                                evidence={
                                    "page_number": page_number,
                                    "table_count": len(tables),
                                },
                            )
                        )
        finally:
            pdf_document.close()
        result.warnings.append(
            {
                "candidate_status": "candidate",
                "ocr_enabled": False,
                "pdf_text_extraction": True,
                "config": candidate_config.as_dict(),
                "pages": [int(index) + 1 for index in page_indexes],
            }
        )
        result.status = "partial" if result.issues else "successful"
    except Exception as exc:  # isolate one candidate/native parser failure
        result.status = "failed"
        result.assets.clear()
        result.detected_tables.clear()
        result.issues.clear()
        result.error_category = (
            "missing_dependency"
            if isinstance(exc, (ImportError, ModuleNotFoundError)) or "not provisioned" in str(exc)
            else "table_extractor_error"
        )
        result.error_message = str(exc)
        result.issues.append(
            _issue(
                source=source,
                asset_id=f"pdf-table:{source.file_id}",
                issue_type="table_extractor_error",
                evidence={
                    "error_category": result.error_category,
                    "error": str(exc),
                    "pages": [int(index) + 1 for index in page_indexes],
                },
                severity=QualityIssueSeverity.ERROR,
            )
        )
    finally:
        if result.timings.extraction_ms == 0.0 and result.status == "failed":
            result.timings.extraction_ms = (time.perf_counter_ns() - started) / 1_000_000
    return result
