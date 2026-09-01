"""Atomic Parquet/metadata publication for structured assets."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import time
import unicodedata
from typing import Any, Sequence
from uuid import uuid4

import polars as pl

from chongzu import paths
from chongzu.assets import (
    AssetQualityStatus,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueStatus,
    SourceKind,
    TableAsset,
    make_table_id,
    utc_now,
)

from .models import FileExtractionResult, StructuredSource
from .table_regions import TableRegion, is_empty


def workspace_relative(path: Path, workspace_root: Path) -> str:
    return path.resolve().relative_to(workspace_root.resolve()).as_posix()


def artifact_absolute(relative_path: str, workspace_root: Path = paths.WORKSPACE_ROOT) -> Path:
    candidate = (workspace_root / Path(relative_path)).resolve()
    candidate.relative_to(workspace_root.resolve())
    return candidate


def _atomic_target(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    return target.with_name(f".{uuid4().hex[:12]}.tmp")


def write_json_atomic(target: Path, payload: dict[str, Any]) -> None:
    temporary = _atomic_target(target)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_parquet_atomic(frame: pl.DataFrame | pl.LazyFrame, target: Path) -> float:
    started = time.perf_counter_ns()
    temporary = _atomic_target(target)
    try:
        if isinstance(frame, pl.LazyFrame):
            frame.sink_parquet(temporary, compression="zstd", maintain_order=True)
        else:
            frame.write_parquet(temporary, compression="zstd", use_pyarrow=False)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return (time.perf_counter_ns() - started) / 1_000_000


def positional_names(width: int) -> list[str]:
    return [f"source_col_{index + 1:04d}" for index in range(width)]


def normalize_column_names(values: Sequence[Any], width: int) -> tuple[list[str], list[dict[str, Any]], bool]:
    names: list[str] = []
    mapping: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    duplicate = False
    for index in range(width):
        original = values[index] if index < len(values) else None
        base = unicodedata.normalize("NFC", str(original)).strip() if not is_empty(original) else f"column_{index + 1:04d}"
        key = base.casefold()
        occurrence = seen.get(key, 0) + 1
        seen[key] = occurrence
        normalized = base if occurrence == 1 else f"{base}__{occurrence}"
        duplicate = duplicate or occurrence > 1
        names.append(normalized)
        mapping.append(
            {
                "source_column_offset": index,
                "original": original,
                "normalized": normalized,
                "duplicate_ordinal": occurrence,
            }
        )
    return names, mapping, duplicate


def _typed_series(name: str, values: Sequence[Any]) -> pl.Series:
    present = [value for value in values if value is not None]
    if not present:
        return pl.Series(name, values, dtype=pl.String)
    if all(isinstance(value, bool) for value in present):
        return pl.Series(name, values, dtype=pl.Boolean, strict=False)
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return pl.Series(name, values, dtype=pl.Int64, strict=False)
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        return pl.Series(name, values, dtype=pl.Float64, strict=False)
    if all(isinstance(value, datetime) for value in present):
        return pl.Series(name, values, dtype=pl.Datetime, strict=False)
    if all(isinstance(value, date) and not isinstance(value, datetime) for value in present):
        return pl.Series(name, values, dtype=pl.Date, strict=False)
    converted = [None if value is None else unicodedata.normalize("NFC", str(value)) for value in values]
    return pl.Series(name, converted, dtype=pl.String)


def matrix_frame(rows: Sequence[Sequence[Any]], names: Sequence[str] | None = None) -> pl.DataFrame:
    width = len(names) if names is not None else max((len(row) for row in rows), default=0)
    column_names = list(names or positional_names(width))
    padded = [list(row[:width]) + [None] * max(0, width - len(row)) for row in rows]
    return pl.DataFrame([_typed_series(name, [row[index] for row in padded]) for index, name in enumerate(column_names)])


def make_issue(
    *, asset_id: str, issue_type: str, evidence: dict[str, Any], severity: QualityIssueSeverity = QualityIssueSeverity.WARNING
) -> QualityIssue:
    identity = json.dumps([asset_id, issue_type, evidence], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return QualityIssue(
        issue_id=f"issue_{hashlib.sha256(identity).hexdigest()[:32]}",
        asset_id=asset_id,
        severity=severity,
        issue_type=issue_type,
        description=issue_type.replace("_", " "),
        evidence=evidence,
        detected_by="structured-v1-deterministic",
        suggested_action="Review the source boundary or header before semantic cleaning.",
        status=QualityIssueStatus.OPEN,
    )


def write_sheet_snapshot(
    source: StructuredSource,
    sheet_index: int,
    sheet_name: str,
    rows: Sequence[Sequence[Any]],
    result: FileExtractionResult,
) -> str:
    target = source.workspace_root / "artifacts" / "sheets" / source.file_id / source.content_sha256[:16] / f"sheet-{sheet_index + 1:04d}" / "raw.parquet"
    metadata = target.with_name("metadata.json")
    width = max((len(row) for row in rows), default=0)
    if width:
        result.timings.parquet_write_ms += write_parquet_atomic(matrix_frame(rows), target)
    write_json_atomic(
        metadata,
        {
            "contract_version": paths.STRUCTURED_CONFIG_VERSION,
            "file_id": source.file_id,
            "content_sha256": source.content_sha256,
            "source_relative_path": source.relative_path,
            "sheet_index": sheet_index,
            "sheet_name": sheet_name,
            "row_count": len(rows),
            "column_count": width,
            "raw_artifact": workspace_relative(target, source.workspace_root) if width else None,
            "coordinate_system": "zero-based half-open",
        },
    )
    return workspace_relative(target if width else metadata, source.workspace_root)


def publish_matrix_region(
    *,
    source: StructuredSource,
    result: FileExtractionResult,
    rows: Sequence[Sequence[Any]],
    region: TableRegion,
    asset_index: int,
    sheet_name: str | None,
    sheet_index: int | None,
    full_sheet_artifact: str | None,
    known_header: Sequence[Any] | None = None,
    data_source_row_offset: int | None = None,
) -> TableAsset:
    source_locator = (
        f"sheet:{sheet_index}:{sheet_name}:" if sheet_index is not None else "file:"
    ) + (
        f"r{region.row_start}:{region.row_end}:c{region.column_start}:{region.column_end}:"
        f"config:{paths.STRUCTURED_CONFIG_VERSION}"
    )
    table_id = make_table_id(
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extractor=result.extractor,
        extractor_version=result.extractor_version,
        source_kind=SourceKind.SHEET if sheet_name is not None else SourceKind.FILE,
        source_locator=source_locator,
        asset_index=asset_index,
    )
    target_dir = source.workspace_root / "artifacts" / "tables" / table_id
    raw_path = target_dir / "raw.parquet"
    normalized_path = target_dir / "normalized.parquet"
    metadata_path = target_dir / "metadata.json"
    region_rows = [
        list(row[region.column_start : region.column_end])
        for row in rows[region.row_start : region.row_end]
    ]
    width = region.column_end - region.column_start
    raw_frame = matrix_frame(region_rows)
    result.timings.parquet_write_ms += write_parquet_atomic(raw_frame, raw_path)

    warnings = set(region.warnings)
    header = list(known_header) if known_header is not None else None
    data_rows = region_rows
    normalized_source_row_start = region.row_start
    if header is None and region_rows:
        first = region_rows[0]
        nonempty = [value for value in first if not is_empty(value)]
        header_likely = (
            len(nonempty) >= 2
            and all(isinstance(value, str) for value in nonempty)
            and "possible_title_row" not in warnings
            and "possible_multirow_header" not in warnings
        )
        if header_likely:
            header = first
            data_rows = region_rows[1:]
            normalized_source_row_start += 1
    elif header is not None:
        normalized_source_row_start = data_source_row_offset if data_source_row_offset is not None else region.row_start + 1

    normalization_started = time.perf_counter_ns()
    names, column_mapping, duplicate = normalize_column_names(header or (), width)
    if duplicate:
        warnings.add("duplicate_column_names")
    normalized_frame = matrix_frame(data_rows, names)
    result.timings.normalization_ms += (time.perf_counter_ns() - normalization_started) / 1_000_000
    result.timings.parquet_write_ms += write_parquet_atomic(normalized_frame, normalized_path)

    quality_status = AssetQualityStatus.REVIEW if warnings else AssetQualityStatus.PASS
    metadata = {
        "contract_version": paths.STRUCTURED_CONFIG_VERSION,
        "table_id": table_id,
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "source_relative_path": source.relative_path,
        "source_kind": "sheet" if sheet_name is not None else "file",
        "sheet_index": sheet_index,
        "sheet_name": sheet_name,
        "source_range": {
            "row_start": region.row_start,
            "row_end": region.row_end,
            "column_start": region.column_start,
            "column_end": region.column_end,
            "coordinate_system": "zero-based half-open",
        },
        "normalized_row_mapping": {
            "parquet_row_zero_maps_to_source_row": normalized_source_row_start,
            "logical_records": sheet_name is None,
        },
        "column_mapping": column_mapping,
        "raw_columns": positional_names(width),
        "normalized_columns": names,
        "full_sheet_raw_artifact": full_sheet_artifact,
        "extractor": result.extractor,
        "extractor_version": result.extractor_version,
        "extraction_run_id": result.extraction_run_id,
        "quality_warnings": sorted(warnings),
        "layers": {"raw": "raw.parquet", "normalized": "normalized.parquet", "semantic": None},
    }
    write_json_atomic(metadata_path, metadata)
    asset = TableAsset(
        table_id=table_id,
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extraction_run_id=result.extraction_run_id,
        extractor=result.extractor,
        extractor_version=result.extractor_version,
        source_kind=SourceKind.SHEET if sheet_name is not None else SourceKind.FILE,
        source_relative_path=source.relative_path,
        sheet_name=sheet_name,
        page_number=None,
        bbox=None,
        source_row_start=region.row_start,
        source_row_end=region.row_end,
        source_column_start=region.column_start,
        source_column_end=region.column_end,
        row_count=normalized_frame.height,
        column_count=normalized_frame.width,
        columns=tuple(names),
        raw_artifact_path=workspace_relative(raw_path, source.workspace_root),
        normalized_artifact_path=workspace_relative(normalized_path, source.workspace_root),
        metadata_artifact_path=workspace_relative(metadata_path, source.workspace_root),
        extraction_confidence=1.0 if not warnings else 0.75,
        quality_status=quality_status,
        created_at=utc_now(),
    )
    for warning in sorted(warnings):
        result.issues.append(
            make_issue(
                asset_id=table_id,
                issue_type=warning,
                evidence={
                    "source_relative_path": source.relative_path,
                    "sheet_name": sheet_name,
                    "source_range": metadata["source_range"],
                },
            )
        )
    return asset


def publish_csv_table(
    *,
    source: StructuredSource,
    result: FileExtractionResult,
    utf8_path: Path,
    delimiter: str,
    header: Sequence[str],
    logical_record_count: int,
) -> TableAsset:
    """Stream a validated delimited file into raw and normalized Parquet."""

    width = len(header)
    table_id = make_table_id(
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extractor=result.extractor,
        extractor_version=result.extractor_version,
        source_kind=SourceKind.FILE,
        source_locator=(
            f"file:r0:{logical_record_count}:c0:{width}:config:{paths.STRUCTURED_CONFIG_VERSION}"
        ),
        asset_index=0,
    )
    target_dir = source.workspace_root / "artifacts" / "tables" / table_id
    raw_path = target_dir / "raw.parquet"
    normalized_path = target_dir / "normalized.parquet"
    metadata_path = target_dir / "metadata.json"
    raw_names = positional_names(width)
    names, column_mapping, duplicate = normalize_column_names(header, width)

    common = {
        "source": utf8_path,
        "separator": delimiter,
        "has_header": True,
        "infer_schema": False,
        "ignore_errors": False,
        "truncate_ragged_lines": False,
        "low_memory": True,
    }
    raw_lazy = pl.scan_csv(**common, new_columns=raw_names)
    normalization_started = time.perf_counter_ns()
    normalized_lazy = pl.scan_csv(**common, new_columns=names).with_columns(
        [pl.col(name).str.strip_chars().str.normalize("NFC").alias(name) for name in names]
    )
    result.timings.normalization_ms += (time.perf_counter_ns() - normalization_started) / 1_000_000
    result.timings.parquet_write_ms += write_parquet_atomic(raw_lazy, raw_path)
    result.timings.parquet_write_ms += write_parquet_atomic(normalized_lazy, normalized_path)

    metadata = {
        "contract_version": paths.STRUCTURED_CONFIG_VERSION,
        "table_id": table_id,
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "source_relative_path": source.relative_path,
        "source_kind": "file",
        "source_range": {
            "row_start": 0,
            "row_end": logical_record_count,
            "column_start": 0,
            "column_end": width,
            "coordinate_system": "zero-based half-open logical CSV records",
        },
        "normalized_row_mapping": {
            "parquet_row_zero_maps_to_source_row": 1,
            "logical_records": True,
            "note": "quoted multiline cells occupy one logical source record",
        },
        "column_mapping": column_mapping,
        "raw_columns": raw_names,
        "normalized_columns": names,
        "extractor": result.extractor,
        "extractor_version": result.extractor_version,
        "extraction_run_id": result.extraction_run_id,
        "quality_warnings": ["duplicate_column_names"] if duplicate else [],
        "layers": {"raw": "raw.parquet", "normalized": "normalized.parquet", "semantic": None},
    }
    write_json_atomic(metadata_path, metadata)
    asset = TableAsset(
        table_id=table_id,
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extraction_run_id=result.extraction_run_id,
        extractor=result.extractor,
        extractor_version=result.extractor_version,
        source_kind=SourceKind.FILE,
        source_relative_path=source.relative_path,
        sheet_name=None,
        page_number=None,
        bbox=None,
        source_row_start=0,
        source_row_end=logical_record_count,
        source_column_start=0,
        source_column_end=width,
        row_count=max(0, logical_record_count - 1),
        column_count=width,
        columns=tuple(names),
        raw_artifact_path=workspace_relative(raw_path, source.workspace_root),
        normalized_artifact_path=workspace_relative(normalized_path, source.workspace_root),
        metadata_artifact_path=workspace_relative(metadata_path, source.workspace_root),
        extraction_confidence=1.0 if not duplicate else 0.9,
        quality_status=AssetQualityStatus.REVIEW if duplicate else AssetQualityStatus.PASS,
        created_at=utc_now(),
    )
    if duplicate:
        result.issues.append(
            make_issue(
                asset_id=table_id,
                issue_type="duplicate_column_names",
                evidence={"source_relative_path": source.relative_path, "column_mapping": column_mapping},
            )
        )
    return asset
