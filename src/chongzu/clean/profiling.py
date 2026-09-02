"""Low-cost, bounded deterministic profiles for cleaned assets."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

import polars as pl

from chongzu import paths


MAX_SAMPLE_VALUES = 5
LONG_TEXT_LENGTH = 200


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _scalar(frame: pl.DataFrame, column: str) -> Any:
    return frame.get_column(column)[0]


def _count_true(values: pl.Series) -> int:
    return int(values.fill_null(False).sum())


def _column_profile(frame: pl.DataFrame, name: str, hint: Mapping[str, Any] | None = None) -> dict[str, Any]:
    series = frame.get_column(name)
    non_null = series.len() - series.null_count()
    distinct = int(series.drop_nulls().n_unique()) if non_null else 0
    values = series.drop_nulls().head(MAX_SAMPLE_VALUES).to_list()
    result: dict[str, Any] = {
        "name": name,
        "null_count": int(series.null_count()),
        "null_ratio": (series.null_count() / series.len()) if series.len() else 0.0,
        "distinct_count": distinct,
        "inferred_physical_type": str(series.dtype),
        "sample_values": [_json_value(value) for value in values],
        "possible_identifier_hint": bool((hint or {}).get("possible_identifier_hint", False)),
        "possible_constant_column": bool(non_null > 0 and distinct <= 1),
        "possible_high_cardinality_column": bool(non_null >= 10 and distinct / non_null >= 0.9),
    }
    if non_null and series.dtype in {pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64, pl.Float32, pl.Float64}:
        result["min"] = _json_value(series.min())
        result["max"] = _json_value(series.max())
        result["mean"] = _json_value(series.mean())
        result["median"] = _json_value(series.median())
    elif non_null and series.dtype in {pl.Date, pl.Datetime, pl.Time}:
        result["min"] = _json_value(series.min())
        result["max"] = _json_value(series.max())
    return result


def profile_table(
    frame: pl.DataFrame,
    candidate: Mapping[str, Any],
    *,
    profile_id: str,
    cleaning_identity: str,
    raw_row_count: int,
    raw_column_count: int,
    empty_row_count_before: int,
    empty_column_count_before: int,
    exact_duplicate_row_count: int,
    column_hints: Mapping[str, Mapping[str, Any]] | None,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    row_count = frame.height
    column_count = frame.width
    total_cells = row_count * column_count
    null_count = sum(frame.get_column(name).null_count() for name in frame.columns)
    long_text_cells = 0
    for name in frame.columns:
        long_text_cells += _count_true(
            frame.get_column(name).cast(pl.String, strict=False).str.len_chars() > LONG_TEXT_LENGTH
        )
    raw_widths = metadata.get("raw_row_widths")
    irregular = isinstance(raw_widths, list) and len(set(int(item) for item in raw_widths)) > 1
    confidence_values: list[float] = []
    block_values = metadata.get("ocr_blocks")
    if isinstance(block_values, list):
        for block in block_values:
            if isinstance(block, Mapping) and block.get("confidence") is not None:
                try:
                    confidence_values.append(float(block["confidence"]))
                except (TypeError, ValueError):
                    continue
    confidence = candidate.get("extraction_confidence")
    if confidence is None:
        confidence = metadata.get("ocr_mean_confidence", metadata.get("average_confidence"))
    confidence_value = float(confidence) if confidence is not None else None
    metadata_min = metadata.get("ocr_min_confidence")
    confidence_min = min(confidence_values) if confidence_values else (
        float(metadata_min) if metadata_min is not None else confidence_value
    )
    profile = {
        "profile_version": paths.PROFILE_CONFIG_VERSION,
        "profile_id": profile_id,
        "asset_id": str(candidate["asset_id"]),
        "asset_type": "table",
        "cleaning_identity": cleaning_identity,
        "source_relative_path": str(candidate.get("source_relative_path") or ""),
        "source_format": str(candidate.get("business_format") or ""),
        "extractor": str(candidate.get("extractor") or ""),
        "extractor_version": str(candidate.get("extractor_version") or ""),
        "source_kind": str(candidate.get("source_kind") or ""),
        "row_count": row_count,
        "column_count": column_count,
        "raw_row_count": raw_row_count,
        "raw_column_count": raw_column_count,
        "null_count": int(null_count),
        "null_ratio": (null_count / total_cells) if total_cells else 0.0,
        "empty_cell_ratio": (null_count / total_cells) if total_cells else 0.0,
        "long_text_cell_count": int(long_text_cells),
        "long_text_cell_ratio": (long_text_cells / total_cells) if total_cells else 0.0,
        "empty_row_count_before": int(empty_row_count_before),
        "empty_column_count_before": int(empty_column_count_before),
        "exact_duplicate_row_count": int(exact_duplicate_row_count),
        "irregular_row_width": irregular,
        "ocr_block_count": len(confidence_values) if confidence_values else int(metadata.get("ocr_block_count") or 0),
        "ocr_mean_confidence": confidence_value if str(candidate.get("extractor") or "").lower().find("ocr") >= 0 or candidate.get("source_kind") in {"image", "page"} else None,
        "ocr_min_confidence": confidence_min if str(candidate.get("extractor") or "").lower().find("ocr") >= 0 or candidate.get("source_kind") in {"image", "page"} else None,
        "provenance_complete": all(
            bool(candidate.get(key))
            for key in ("asset_id", "file_id", "content_sha256", "extraction_run_id", "extractor", "extractor_version", "source_relative_path", "source_kind", "raw_artifact_path")
        ),
        "source_quality_warnings": list(metadata.get("quality_warnings") or []),
        "columns": [
            _column_profile(frame, name, (column_hints or {}).get(name))
            for name in frame.columns
        ],
    }
    return profile


def profile_text(
    text: str,
    candidate: Mapping[str, Any],
    *,
    profile_id: str,
    cleaning_identity: str,
    metadata: Mapping[str, Any],
    chunk_count: int,
) -> dict[str, Any]:
    char_count = len(text)
    line_count = text.count("\n") + 1 if text else 0
    block_values = metadata.get("ocr_blocks")
    block_count = len(block_values) if isinstance(block_values, list) else int(metadata.get("block_count") or 0)
    page_count = 1 if candidate.get("page_number") is not None else int(metadata.get("page_count") or 1)
    extractor = str(candidate.get("extractor") or "")
    extraction_source = "ocr" if "ocr" in extractor.casefold() else "native"
    confidence = metadata.get("average_confidence")
    profile = {
        "profile_version": paths.PROFILE_CONFIG_VERSION,
        "profile_id": profile_id,
        "asset_id": str(candidate["asset_id"]),
        "asset_type": "text",
        "cleaning_identity": cleaning_identity,
        "source_relative_path": str(candidate.get("source_relative_path") or ""),
        "source_format": str(candidate.get("business_format") or ""),
        "extractor": extractor,
        "extractor_version": str(candidate.get("extractor_version") or ""),
        "source_kind": str(candidate.get("source_kind") or ""),
        "char_count": char_count,
        "line_count": line_count,
        "page_count": page_count,
        "block_count": block_count,
        "chunk_count": int(chunk_count),
        "language_hint": candidate.get("language"),
        "extraction_source": extraction_source,
        "ocr_mean_confidence": float(confidence) if confidence is not None else None,
        "empty_content": not bool(text.strip()),
        "low_content": len(text.strip()) < 20,
        "provenance_complete": all(
            bool(candidate.get(key))
            for key in ("asset_id", "file_id", "content_sha256", "extraction_run_id", "extractor", "extractor_version", "source_relative_path", "source_kind", "raw_artifact_path")
        ),
    }
    return profile
