"""Conservative, columnar table normalization from raw Parquet artifacts."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping
import unicodedata

import polars as pl

from dongjian import paths
from dongjian.extract.artifacts import (
    artifact_absolute,
    workspace_relative,
    write_json_atomic,
    write_parquet_atomic,
)

from .models import CleaningAssetResult
from .profiling import profile_table


CLEANER_NAME = "deterministic-table-cleaner"
CLEANER_VERSION = paths.TABLE_CLEANER_VERSION
NULL_TOKENS = frozenset({"", "null", "n/a", "na"})
INTEGER_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d+|\d+\.?\d*[eE][+-]?\d+)$")
DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
DATETIME_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}[T ]\d{1,2}:\d{1,2}(?::\d{1,2}(?:\.\d+)?)?$")
MAX_SAFE_INTEGER_DIGITS = 7


def _action(actions: list[dict[str, Any]], action_type: str, count: int, **details: Any) -> None:
    if count <= 0:
        return
    item: dict[str, Any] = {"type": action_type, "count": int(count)}
    item.update(details)
    actions.append(item)


def _empty_row_count(frame: pl.DataFrame) -> int:
    if frame.height == 0 or frame.width == 0:
        return frame.height
    markers = [
        pl.col(name).is_null()
        | pl.col(name).cast(pl.String, strict=False).str.strip_chars().eq("")
        for name in frame.columns
    ]
    return int(frame.select(pl.all_horizontal(markers).sum()).item())


def _empty_column_names(frame: pl.DataFrame) -> list[str]:
    result: list[str] = []
    for name in frame.columns:
        if not bool(frame.get_column(name).is_not_null().any()):
            result.append(name)
    return result


def _mechanical_name(value: Any, index: int) -> str:
    if value is None:
        text = ""
    else:
        text = unicodedata.normalize("NFC", str(value)).strip()
    text = re.sub(r"\s+", "_", text).strip("_")
    return text or f"column_{index + 1:04d}"


def _original_column_names(raw_names: list[str], metadata: Mapping[str, Any]) -> list[Any]:
    mappings = metadata.get("column_mapping")
    by_offset = {
        int(item.get("source_column_offset")): item
        for item in mappings
        if isinstance(item, dict) and item.get("source_column_offset") is not None
    } if isinstance(mappings, list) else {}
    values: list[Any] = []
    for index, raw_name in enumerate(raw_names):
        item = by_offset.get(index, {})
        values.append(item.get("original", item.get("raw", raw_name)))
    return values


def _normalized_expressions(frame: pl.DataFrame) -> tuple[list[pl.Expr], int]:
    expressions: list[pl.Expr] = []
    normalization_changes = 0
    for name in frame.columns:
        original = pl.col(name)
        original_text = original.cast(pl.String, strict=False)
        text = (
            original_text
            .str.normalize("NFC")
            .str.replace_all(r"\r\n|\r", "\n")
            .str.strip_chars()
        )
        null_marker = text.str.to_lowercase().is_in(list(NULL_TOKENS))
        normalization_changed = (
            ((original_text.fill_null("") != text.fill_null("")) | null_marker.fill_null(False))
            & original.is_not_null()
        )
        change_count = frame.select(normalization_changed.fill_null(False).sum()).item()
        normalization_changes += int(change_count or 0)
        expressions.append(
            pl.when(original.is_null() | null_marker)
            .then(pl.lit(None, dtype=pl.String))
            .otherwise(text)
            .alias(name)
        )
    return expressions, normalization_changes


def _all_true(series: pl.Series, expected_count: int) -> bool:
    return expected_count > 0 and int(series.fill_null(False).sum()) == expected_count


def _parse_date(series: pl.Series) -> tuple[str | None, str | None]:
    non_null = series.len() - series.null_count()
    if non_null == 0:
        return None, None
    values = series.str.replace_all("/", "-")
    if _all_true(series.str.contains(DATE_RE.pattern), non_null):
        try:
            parsed = values.str.strptime(pl.Date, format="%Y-%m-%d", strict=False)
            if parsed.drop_nulls().len() == non_null:
                return "date", "%Y-%m-%d"
        except Exception:
            pass
    if _all_true(series.str.contains(DATETIME_RE.pattern), non_null):
        normalized = values.str.replace_all("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = normalized.str.strptime(pl.Datetime, format=fmt, strict=False)
                if parsed.drop_nulls().len() == non_null:
                    return "datetime", fmt
            except Exception:
                continue
    return None, None


def _infer_column(series: pl.Series) -> tuple[str, pl.Expr, dict[str, Any]]:
    non_null = series.len() - series.null_count()
    hint: dict[str, Any] = {"possible_identifier_hint": False, "risk_reasons": []}
    if non_null == 0:
        return "string", pl.col(series.name).cast(pl.String, strict=False), hint
    lower = series.str.to_lowercase()
    if _all_true(lower.is_in(["true", "false"]), non_null):
        boolean_expression = (
            pl.when(pl.col(series.name).str.to_lowercase() == "true")
            .then(pl.lit(True))
            .when(pl.col(series.name).str.to_lowercase() == "false")
            .then(pl.lit(False))
            .otherwise(None)
        )
        return "boolean_candidate", boolean_expression, hint

    digit_mask = series.str.contains(INTEGER_RE.pattern)
    leading_zero_mask = series.str.contains(r"^[+-]?0\d+$")
    long_integer_mask = digit_mask & (series.str.len_chars() > MAX_SAFE_INTEGER_DIGITS)
    identifier_risk = bool(leading_zero_mask.fill_null(False).any() or long_integer_mask.fill_null(False).any())
    if identifier_risk:
        hint["possible_identifier_hint"] = True
        if bool(leading_zero_mask.fill_null(False).any()):
            hint["risk_reasons"].append("leading_zero_numeric_text")
        if bool(long_integer_mask.fill_null(False).any()):
            hint["risk_reasons"].append("long_numeric_identifier_shape")

    date_type, date_format = _parse_date(series)
    if date_type and date_format is not None:
        if date_type == "date":
            expression = pl.col(series.name).str.replace_all("/", "-").str.strptime(
                pl.Date, format=date_format, strict=False
            )
        else:
            expression = (
                pl.col(series.name)
                .str.replace_all("/", "-")
                .str.replace_all("T", " ")
                .str.strptime(pl.Datetime, format=date_format, strict=False)
            )
        return date_type, expression, hint

    if _all_true(digit_mask, non_null) and not identifier_risk:
        return "integer", pl.col(series.name).cast(pl.Int64, strict=False), hint
    numeric = series.cast(pl.Float64, strict=False)
    numeric_mask = numeric.is_not_null()
    if _all_true(numeric_mask, non_null) and not bool((series.str.len_chars() > 32).fill_null(False).any()):
        if not identifier_risk:
            return "float_candidate", pl.col(series.name).cast(pl.Float64, strict=False), hint
    return "string", pl.col(series.name).cast(pl.String, strict=False), hint


def _typed_frame(frame: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, dict[str, Any]], int]:
    expressions: list[pl.Expr] = []
    hints: dict[str, dict[str, Any]] = {}
    inferred_count = 0
    for name in frame.columns:
        inferred_type, expression, hint = _infer_column(frame.get_column(name))
        expressions.append(expression.alias(name))
        hints[name] = {"inferred_type": inferred_type, **hint}
        if inferred_type != "string":
            inferred_count += 1
    return frame.select(expressions), hints, inferred_count


def _column_mapping(raw_names: list[str], original_names: list[Any]) -> tuple[list[str], list[dict[str, Any]], int]:
    names: list[str] = []
    mappings: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    changed = 0
    for index, (raw_name, original) in enumerate(zip(raw_names, original_names, strict=True)):
        base = _mechanical_name(original, index)
        key = base.casefold()
        ordinal = seen.get(key, 0) + 1
        seen[key] = ordinal
        final = base if ordinal == 1 else f"{base}__{ordinal}"
        changed += int(final != raw_name or (original is not None and str(original) != final))
        names.append(final)
        mappings.append(
            {
                "source_column_offset": index,
                "raw": raw_name,
                "original": original,
                "normalized": final,
                "duplicate_ordinal": ordinal,
            }
        )
    return names, mappings, changed


def _profile_row(profile: Mapping[str, Any], profile_id: str, content_sha256: str) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "table_id": profile["asset_id"],
        "content_sha256": content_sha256,
        "row_count": int(profile["row_count"]),
        "column_count": int(profile["column_count"]),
        "null_count": int(profile["null_count"]),
        "null_ratio": float(profile["null_ratio"]),
        "empty_row_count_before": int(profile["empty_row_count_before"]),
        "empty_column_count_before": int(profile["empty_column_count_before"]),
        "exact_duplicate_row_count": int(profile["exact_duplicate_row_count"]),
        "empty_cell_ratio": float(profile["empty_cell_ratio"]),
        "long_text_cell_ratio": float(profile["long_text_cell_ratio"]),
        "irregular_row_width": bool(profile["irregular_row_width"]),
        "ocr_mean_confidence": profile.get("ocr_mean_confidence"),
        "ocr_min_confidence": profile.get("ocr_min_confidence"),
        "provenance_complete": bool(profile["provenance_complete"]),
    }


def clean_table(
    candidate: Mapping[str, Any],
    *,
    workspace_root: Path,
    raw_artifact_identity: str,
    cleaning_identity: str,
    cleaning_run_id: str,
    drop_exact_duplicates: bool = False,
) -> CleaningAssetResult:
    result = CleaningAssetResult(
        asset_id=str(candidate["asset_id"]),
        asset_type="table",
        file_id=str(candidate["file_id"]),
        content_sha256=str(candidate["content_sha256"]),
        source_root=str(candidate["source_root"]),
        source_relative_path=str(candidate["source_relative_path"] or ""),
        raw_artifact_identity=raw_artifact_identity,
        cleaner=CLEANER_NAME,
        cleaner_version=paths.TABLE_CLEANER_VERSION,
        config_version=paths.CLEANING_CONFIG_VERSION,
        cleaning_run_id=cleaning_run_id,
        cleaning_identity=cleaning_identity,
    )
    try:
        raw_path = artifact_absolute(str(candidate["raw_artifact_path"]), workspace_root)
        metadata_path = artifact_absolute(str(candidate["metadata_artifact_path"]), workspace_root)
        raw_frame = pl.read_parquet(raw_path)
        metadata: dict[str, Any] = {}
        if metadata_path.is_file():
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                metadata = payload
        if isinstance(metadata.get("quality_warnings"), list):
            result.warnings = [{"type": str(item)} for item in metadata["quality_warnings"]]
        actions: list[dict[str, Any]] = []
        raw_row_count = raw_frame.height
        raw_column_count = raw_frame.width
        empty_rows_before = _empty_row_count(raw_frame)
        normalize_started = time.perf_counter_ns()
        expressions, normalization_changes = _normalized_expressions(raw_frame)
        normalized = raw_frame.select(expressions)
        result.timings.normalize_ms += (time.perf_counter_ns() - normalize_started) / 1_000_000
        _action(actions, "normalize_unicode_whitespace_newlines_nulls", normalization_changes)
        empty_columns_before = _empty_column_names(normalized)
        original_names = _original_column_names(raw_frame.columns, metadata)
        cleaned_names, column_mapping, renamed_count = _column_mapping(raw_frame.columns, original_names)
        for mapping in column_mapping:
            raw_name = str(mapping["raw"])
            normalized_name = str(mapping["normalized"])
            mapping["original_inferred_representation"] = str(raw_frame.get_column(raw_name).dtype)
        normalized = normalized.rename(dict(zip(raw_frame.columns, cleaned_names, strict=True)))
        _action(actions, "mechanical_column_name_normalization", renamed_count)
        empty_row_mask = [pl.col(name).is_null() for name in normalized.columns]
        empty_rows_after_trim = int(normalized.select(pl.all_horizontal(empty_row_mask).sum()).item()) if normalized.width else normalized.height
        if empty_rows_after_trim:
            normalized = normalized.filter(~pl.all_horizontal(empty_row_mask))
        _action(actions, "trim_empty_rows", empty_rows_after_trim)
        empty_columns = _empty_column_names(normalized)
        if empty_columns:
            normalized = normalized.drop(empty_columns)
        _action(actions, "trim_empty_columns", len(empty_columns), columns=empty_columns[:100])

        duplicate_rows = int(normalized.is_duplicated().sum()) if normalized.height else 0
        _action(actions, "mark_exact_duplicate_rows", duplicate_rows)
        if drop_exact_duplicates and duplicate_rows:
            before_dedupe = normalized.height
            normalized = normalized.unique(maintain_order=True)
            _action(actions, "drop_exact_duplicate_rows", before_dedupe - normalized.height)
        typed, hints, inferred_count = _typed_frame(normalized) if normalized.width else (normalized, {}, 0)
        for mapping in column_mapping:
            mapping["normalized_inferred_type"] = hints.get(str(mapping["normalized"]), {}).get("inferred_type", "string")
        _action(actions, "conservative_type_inference", inferred_count)
        profile_id = f"tprof_{hashlib.sha256((cleaning_identity + ':' + cleaning_run_id + ':profile').encode('utf-8')).hexdigest()[:32]}"
        profile_started = time.perf_counter_ns()
        profile = profile_table(
            typed,
            candidate,
            profile_id=profile_id,
            cleaning_identity=cleaning_identity,
            raw_row_count=raw_row_count,
            raw_column_count=raw_column_count,
            empty_row_count_before=empty_rows_before,
            empty_column_count_before=len(empty_columns_before),
            exact_duplicate_row_count=duplicate_rows,
            column_hints=hints,
            metadata=metadata,
        )
        result.timings.profile_ms += (time.perf_counter_ns() - profile_started) / 1_000_000
        profile["profile_id"] = profile_id
        profile["column_mapping"] = column_mapping
        profile["cleaning_actions"] = actions
        target_dir = workspace_root / "artifacts" / "cleaning" / "tables" / str(candidate["asset_id"]) / cleaning_identity[:16]
        normalized_path = target_dir / "normalized.parquet" if typed.width else None
        manifest_path = target_dir / "cleaning.json"
        profile_path = target_dir / "profile.json"
        if normalized_path is not None:
            result.timings.parquet_write_ms += write_parquet_atomic(typed, normalized_path)
        manifest = {
            "cleaning_version": paths.TABLE_CLEANER_VERSION,
            "config_version": paths.CLEANING_CONFIG_VERSION,
            "profile_version": paths.PROFILE_CONFIG_VERSION,
            "cleaning_identity": cleaning_identity,
            "cleaning_run_id": cleaning_run_id,
            "asset_id": str(candidate["asset_id"]),
            "asset_type": "table",
            "file_id": str(candidate["file_id"]),
            "content_sha256": str(candidate["content_sha256"]),
            "raw_artifact_identity": raw_artifact_identity,
            "raw_artifact_path": str(candidate["raw_artifact_path"]),
            "normalized_artifact_path": workspace_relative(normalized_path, workspace_root) if normalized_path else None,
            "drop_exact_duplicates": bool(drop_exact_duplicates),
            "actions": actions,
            "column_mapping": column_mapping,
            "inference_hints": hints,
            "layers": {"raw": str(candidate["raw_artifact_path"]), "normalized": "normalized.parquet" if normalized_path else None, "semantic": None},
        }
        artifact_started = time.perf_counter_ns()
        write_json_atomic(manifest_path, manifest)
        write_json_atomic(profile_path, profile)
        result.timings.artifact_write_ms += (time.perf_counter_ns() - artifact_started) / 1_000_000
        result.normalized_artifact_path = workspace_relative(normalized_path, workspace_root) if normalized_path else None
        result.manifest_artifact_path = workspace_relative(manifest_path, workspace_root)
        result.profile_artifact_path = workspace_relative(profile_path, workspace_root)
        result.profile = profile
        result.profile_row = _profile_row(profile, profile_id, result.content_sha256)
    except Exception as exc:  # per-asset isolation; raw extraction remains untouched
        result.status = "failed"
        result.profile = {}
        result.profile_row = {}
        result.error_category = "table_cleaning_error"
        result.error_message = str(exc)
    return result
