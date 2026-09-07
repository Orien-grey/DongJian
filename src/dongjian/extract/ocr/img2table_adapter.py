"""Image-table adapter backed by one already completed RapidOCR pass.

``img2table`` 2.0.0 exposes a RapidOCR backend, but constructing that backend
would run a second OCR pass.  Its public ``Image(..., ocr_data=...)`` contract
also accepts the library's normalized ``OCRData`` object.  This module is the
small boundary that converts DongJian's stable ``OCRBlock`` values into that
contract and converts the resulting tables back into ``TableAsset`` values.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import statistics
import time
from difflib import SequenceMatcher
from typing import Any, Sequence
import unicodedata

from dongjian import paths
from dongjian.assets import (
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

from ..artifacts import matrix_frame, positional_names, write_json_atomic, write_parquet_atomic
from ..img2table_compat import ensure_img2table_threshold_compat
from ..models import StructuredSource
from ..pdf.table_quality import DetectedTable
from .rapidocr_engine import OCRBlock


IMAGE_TABLE_EXTRACTOR = "img2table-image"
IMAGE_TABLE_EXTRACTOR_VERSION = f"{paths.IMG2TABLE_VERSION}+ocrdata-v1"


@dataclass(frozen=True)
class ImageTableConfig:
    """Conservative image-table settings shared by image and scan routes."""

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
            "ocr_enabled": True,
            "ocr_reuse": "rapidocr_internal_ocrblock_to_img2table_ocrdata",
            "config_version": paths.IMAGE_TABLE_CONFIG_VERSION,
        }


class ImageTableAdapterError(RuntimeError):
    """Raised when the local img2table image adapter cannot run."""


def _bbox_points(block: OCRBlock) -> tuple[float, float, float, float] | None:
    if len(block.bbox) < 4:
        return None
    try:
        xs = [float(point[0]) for point in block.bbox]
        ys = [float(point[1]) for point in block.bbox]
        return min(xs), min(ys), max(xs), max(ys)
    except (IndexError, TypeError, ValueError):
        return None


def ocr_data_from_blocks(blocks: Sequence[OCRBlock], *, page_key: int = 0) -> Any | None:
    """Build img2table's public OCRData shape without exposing it upstream."""

    records: list[dict[str, Any]] = []
    for block in blocks:
        box = _bbox_points(block)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        confidence = int(round((block.confidence if block.confidence is not None else 0.0) * 100))
        records.append(
            {
                "id": f"ocr_block_{block.block_index}",
                "parent": f"ocr_block_{block.block_index}",
                "value": block.text,
                "confidence": max(0, min(100, confidence)),
                "x1": int(round(x1)),
                "y1": int(round(y1)),
                "x2": int(round(x2)),
                "y2": int(round(y2)),
            }
        )
    if not records:
        return None
    try:
        from img2table.ocr._types import OCRData

        return OCRData(records={page_key: records})
    except Exception as exc:  # pragma: no cover - runtime payload dependent
        raise ImageTableAdapterError(f"img2table OCRData adapter failed: {exc}") from exc


def load_image_document(src: Any) -> Any:
    """Create an img2table Image document lazily from a path or encoded bytes."""

    try:
        from img2table.document import Image

        return Image(src, detect_rotation=False)
    except Exception as exc:  # pragma: no cover - runtime payload dependent
        raise ImageTableAdapterError(f"img2table image document could not be opened: {exc}") from exc


def extract_tables_from_document(
    document: Any,
    blocks: Sequence[OCRBlock],
    *,
    config: ImageTableConfig | None = None,
) -> tuple[list[Any], int, int, float]:
    """Run table reconstruction using the OCRData made from *blocks*.

    The document's decoded image is also the array passed to RapidOCR by the
    caller.  ``extract_tables(ocr=None)`` is intentional: the injected
    ``ocr_data`` is consumed by img2table and no OCR backend is instantiated.
    """

    candidate_config = config or ImageTableConfig()
    try:
        ensure_img2table_threshold_compat()
        image = document.images[0]
        height, width = int(image.shape[0]), int(image.shape[1])
        document.ocr_data = ocr_data_from_blocks(blocks)
        started = time.perf_counter_ns()
        tables = document.extract_tables(
            ocr=None,
            implicit_rows=candidate_config.implicit_rows,
            implicit_columns=candidate_config.implicit_columns,
            borderless_tables=candidate_config.borderless_tables,
            min_confidence=candidate_config.min_confidence,
            max_workers=candidate_config.max_workers,
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        return list(tables or []), width, height, elapsed_ms
    except Exception as exc:  # pragma: no cover - runtime payload dependent
        raise ImageTableAdapterError(f"img2table image table extraction failed: {exc}") from exc


def _normal_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value).strip()
    return value


def _table_rows(table: Any) -> list[list[Any]]:
    content = getattr(table, "content", {})
    if not isinstance(content, dict):
        return []
    return [
        [_normal_cell(getattr(cell, "value", None)) for cell in row]
        for row in content.values()
        if isinstance(row, (list, tuple))
    ]


def _bbox_tuple(table: Any) -> tuple[float, float, float, float] | None:
    bbox = getattr(table, "bbox", None)
    try:
        values = (float(bbox.x1), float(bbox.y1), float(bbox.x2), float(bbox.y2))
    except (AttributeError, TypeError, ValueError):
        return None
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _bbox_iou(left: Any, right: Any) -> float:
    first = _bbox_tuple(left)
    second = _bbox_tuple(right)
    if first is None or second is None:
        return 0.0
    left_edge = max(first[0], second[0])
    top_edge = max(first[1], second[1])
    right_edge = min(first[2], second[2])
    bottom_edge = min(first[3], second[3])
    intersection = max(0.0, right_edge - left_edge) * max(0.0, bottom_edge - top_edge)
    if not intersection:
        return 0.0
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / max(1e-9, first_area + second_area - intersection)


def _row_signature(table: Any) -> str:
    rows = _table_rows(table)
    return json.dumps(
        [["" if value is None else unicodedata.normalize("NFC", str(value)).strip().casefold() for value in row] for row in rows],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def filter_image_table_candidates(tables: Sequence[Any]) -> tuple[list[tuple[int, Any]], list[dict[str, Any]]]:
    """Drop only high-confidence duplicate candidates, retaining source indexes.

    Empty candidates are intentionally passed to ``publish_image_table`` so
    the rejected structure still leaves a provenance-bearing quality issue.
    Candidates without bounding boxes are never deduplicated: there is no
    safe spatial basis for deciding that two matrices came from one region.
    """

    kept: list[tuple[int, Any]] = []
    decisions: list[dict[str, Any]] = []
    signatures: list[tuple[Any, str]] = []
    for index, table in enumerate(tables):
        signature = _row_signature(table)
        rows = _table_rows(table)
        has_nonempty_value = any(
            value is not None and str(value).strip()
            for row in rows
            for value in row
        )
        duplicate_of: int | None = None
        if has_nonempty_value:
            for previous_table, previous_signature in signatures:
                if _bbox_iou(previous_table, table) < 0.90:
                    continue
                similarity = SequenceMatcher(None, previous_signature, signature, autojunk=False).ratio()
                if similarity >= 0.92:
                    duplicate_of = next(
                        (previous_index for previous_index, candidate in kept if candidate is previous_table),
                        None,
                    )
                    if duplicate_of is not None:
                        break
        if duplicate_of is not None:
            decisions.append(
                {
                    "candidate_index": index,
                    "decision": "deduplicated",
                    "reason": "high_overlap_high_similarity",
                    "kept_candidate_index": duplicate_of,
                }
            )
            continue
        kept.append((index, table))
        signatures.append((table, signature))
    return kept, decisions


def _table_bbox(
    table: Any,
    *,
    scale_x: float,
    scale_y: float,
) -> BoundingBox | None:
    values = _bbox_tuple(table)
    if values is None:
        return None
    try:
        return BoundingBox(
            values[0] * scale_x,
            values[1] * scale_y,
            values[2] * scale_x,
            values[3] * scale_y,
        )
    except (TypeError, ValueError):
        return None


def _quality_flags(
    table: Any,
    rows: Sequence[Sequence[Any]],
    blocks: Sequence[OCRBlock],
) -> list[str]:
    """Return only conservative, mechanical table-review signals."""

    widths = [len(row) for row in rows]
    width = max(widths, default=0)
    cell_values = [value for row in rows for value in row]
    nonempty = [value for value in cell_values if value is not None and str(value).strip()]
    flags: list[str] = []
    if width <= 1:
        flags.append("suspicious_single_column")
    if len(rows) <= 1:
        flags.append("suspicious_single_row")
    if len(set(widths)) > 1:
        flags.append("possible_column_shift")
    if len(rows) >= 3 and width >= 3 and min(widths) <= max(1, width // 2):
        flags.append("severe_raggedness")
    if cell_values and len(nonempty) < max(2, (len(cell_values) + 1) // 2):
        flags.append("sparse_ocr")
    if nonempty:
        long_values = [value for value in nonempty if len(str(value)) >= 160]
        if len(long_values) / len(nonempty) >= 0.6:
            flags.append("possible_table_structure_loss")
    if rows and not any(value is not None and str(value).strip() for value in rows[0]):
        if any(value is not None and str(value).strip() for row in rows[1:] for value in row):
            flags.append("possible_header_loss")

    cell_boxes = [
        getattr(cell, "bbox", None)
        for row in getattr(table, "content", {}).values()
        for cell in row
        if getattr(cell, "bbox", None) is not None
    ]
    cell_widths = [float(box.x2 - box.x1) for box in cell_boxes]
    cell_heights = [float(box.y2 - box.y1) for box in cell_boxes]
    if len(cell_widths) >= 4:
        median_width = statistics.median(cell_widths)
        if median_width > 0 and any(value > median_width * 1.6 for value in cell_widths):
            flags.append("possible_merged_cells")
    if len(cell_heights) >= 4:
        median_height = statistics.median(cell_heights)
        if median_height > 0 and any(value > median_height * 1.8 for value in cell_heights):
            flags.append("possible_merged_cells")

    confidences = [block.confidence for block in blocks if block.confidence is not None]
    if confidences and sum(confidences) / len(confidences) < 0.65:
        flags.append("low_ocr_confidence")
    if cell_values and not blocks:
        flags.append("sparse_ocr")
    return list(dict.fromkeys(flags))


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
    return QualityIssue(
        issue_id=f"issue_{hashlib.sha256(identity).hexdigest()[:32]}",
        asset_id=asset_id,
        severity=severity,
        issue_type=issue_type,
        description=issue_type.replace("_", " "),
        evidence=evidence,
        detected_by=f"{IMAGE_TABLE_EXTRACTOR}:{IMAGE_TABLE_EXTRACTOR_VERSION}",
        suggested_action="Review the OCR blocks and source image before accepting this candidate table.",
        status=QualityIssueStatus.OPEN,
    )


@dataclass(frozen=True)
class ImageTablePublication:
    asset: TableAsset | None
    detected: DetectedTable | None
    issues: tuple[QualityIssue, ...]
    artifact_write_ms: float = 0.0


def publish_image_table(
    *,
    source: StructuredSource,
    extraction_run_id: str,
    table: Any,
    table_index: int,
    blocks: Sequence[OCRBlock],
    image_width: int,
    image_height: int,
    source_kind: SourceKind,
    page_number: int | None,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    config: ImageTableConfig | None = None,
) -> ImageTablePublication:
    rows = _table_rows(table)
    width = max((len(row) for row in rows), default=0)
    candidate_label = f"image-table:{source.file_id}:page:{page_number or 1}:table:{table_index}"
    has_nonempty_value = any(
        value is not None and str(value).strip()
        for row in rows
        for value in row
    )
    if not rows or width == 0 or not has_nonempty_value:
        return ImageTablePublication(
            asset=None,
            detected=None,
            issues=(
                _issue(
                    source=source,
                    asset_id=candidate_label,
                    issue_type="image_table_extractor_error",
                    evidence={"reason": "empty_table_structure", "page_number": page_number},
                ),
            ),
        )

    candidate_config = config or ImageTableConfig()
    config_digest = hashlib.sha256(
        json.dumps(candidate_config.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    locator = (
        f"image:page:{page_number or 1}:table:{table_index}:"
        f"config:{paths.IMAGE_TABLE_CONFIG_VERSION}:{config_digest}"
    )
    table_id = make_table_id(
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extractor=IMAGE_TABLE_EXTRACTOR,
        extractor_version=IMAGE_TABLE_EXTRACTOR_VERSION,
        source_kind=source_kind,
        source_locator=locator,
        asset_index=table_index,
    )
    target_dir = source.workspace_root / "artifacts" / "tables" / table_id
    raw_path = target_dir / "raw.parquet"
    normalized_path = target_dir / "normalized.parquet"
    metadata_path = target_dir / "metadata.json"
    raw_names = positional_names(width)
    normalized_names = [f"column_{index + 1:04d}" for index in range(width)]
    normalized_rows = [list(row[:width]) + [None] * max(0, width - len(row)) for row in rows]
    artifact_started = time.perf_counter_ns()
    write_parquet_atomic(matrix_frame(rows, raw_names), raw_path)
    write_parquet_atomic(matrix_frame(normalized_rows, normalized_names), normalized_path)

    bbox = _table_bbox(table, scale_x=scale_x, scale_y=scale_y)
    flags = _quality_flags(table, rows, blocks)
    confidence_values = [block.confidence for block in blocks if block.confidence is not None]
    average_confidence = (
        sum(confidence_values) / len(confidence_values) if confidence_values else None
    )
    minimum_confidence = min(confidence_values) if confidence_values else None
    row_widths = [len(row) for row in rows]
    metadata = {
        "contract_version": paths.IMAGE_TABLE_CONFIG_VERSION,
        "table_id": table_id,
        "candidate_status": "candidate",
        "candidate_semantics": "heuristic_hint_not_ground_truth",
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "source_relative_path": source.relative_path,
        "source_kind": source_kind.value,
        "page_number": page_number,
        "table_index": table_index,
        "source_coordinate_system": "image pixels or PDF points after explicit render scaling",
        "source_range": {
            "row_start": 0,
            "row_end": len(rows),
            "column_start": 0,
            "column_end": width,
            "coordinate_system": "zero-based half-open candidate matrix",
        },
        "candidate_bbox": bbox.__dict__ if bbox else None,
        "img2table_bbox_pixels": (
            {
                "x1": int(getattr(getattr(table, "bbox", None), "x1")),
                "y1": int(getattr(getattr(table, "bbox", None), "y1")),
                "x2": int(getattr(getattr(table, "bbox", None), "x2")),
                "y2": int(getattr(getattr(table, "bbox", None), "y2")),
                "image_width": image_width,
                "image_height": image_height,
            }
            if _bbox_tuple(table) is not None
            else None
        ),
        "rows": len(rows),
        "columns": width,
        "raw_row_widths": row_widths,
        "table_title_candidate": getattr(table, "title", None),
        "extractor": IMAGE_TABLE_EXTRACTOR,
        "extractor_version": IMAGE_TABLE_EXTRACTOR_VERSION,
        "extraction_run_id": extraction_run_id,
        "ocr_enabled": True,
        "ocr_reused": True,
        "ocr_backend_calls": 0,
        "ocr_engine": paths.RAPIDOCR_VERSION,
        "ocr_block_count": len(blocks),
        "ocr_block_indices": [block.block_index for block in blocks],
        "ocr_mean_confidence": average_confidence,
        "ocr_min_confidence": minimum_confidence,
        "quality_warnings": flags,
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
    artifact_write_ms = (time.perf_counter_ns() - artifact_started) / 1_000_000
    asset = TableAsset(
        table_id=table_id,
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extraction_run_id=extraction_run_id,
        extractor=IMAGE_TABLE_EXTRACTOR,
        extractor_version=IMAGE_TABLE_EXTRACTOR_VERSION,
        source_kind=source_kind,
        source_relative_path=source.relative_path,
        sheet_name=None,
        page_number=page_number,
        bbox=bbox,
        source_row_start=0,
        source_row_end=len(rows),
        source_column_start=0,
        source_column_end=width,
        row_count=len(normalized_rows),
        column_count=width,
        columns=tuple(normalized_names),
        raw_artifact_path=(target_dir / "raw.parquet").resolve().relative_to(source.workspace_root.resolve()).as_posix(),
        normalized_artifact_path=(target_dir / "normalized.parquet").resolve().relative_to(source.workspace_root.resolve()).as_posix(),
        metadata_artifact_path=(target_dir / "metadata.json").resolve().relative_to(source.workspace_root.resolve()).as_posix(),
        extraction_confidence=average_confidence,
        quality_status=AssetQualityStatus.REVIEW if flags else AssetQualityStatus.PASS,
        created_at=utc_now(),
    )
    detected = DetectedTable(
        page_number=page_number or 1,
        table_index=table_index,
        rows=tuple(tuple(row) for row in rows),
        table_id=table_id,
        bbox=(bbox.x0, bbox.y0, bbox.x1, bbox.y1) if bbox else None,
    )
    evidence = {
        "page_number": page_number,
        "table_index": table_index,
        "row_widths": row_widths,
        "rows": len(rows),
        "columns": width,
        "ocr_block_count": len(blocks),
        "average_confidence": average_confidence,
    }
    issues = tuple(
        _issue(source=source, asset_id=table_id, issue_type=issue_type, evidence=evidence)
        for issue_type in flags
    )
    return ImageTablePublication(
        asset=asset,
        detected=detected,
        issues=issues,
        artifact_write_ms=artifact_write_ms,
    )
