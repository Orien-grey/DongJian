"""Bounded semantic inputs built from normalized artifacts only."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import polars as pl

from .. import paths
from ..extract.artifacts import artifact_absolute
from .models import SemanticRequest
from .prompts import prompt_sections, version_for


MAX_TABLE_SAMPLE_ROWS = 18
MAX_TABLE_CELL_CHARS = 240
MAX_TEXT_EXCERPT_CHARS = 12_000
MAX_REFERENCE_BYTES = 48 * 1024
MAX_PROMPT_BYTES = 64 * 1024
MAX_PROFILE_COLUMNS = 100
MAX_QUALITY_ISSUES = 20


@dataclass(frozen=True)
class SemanticInputLimits:
    table_sample_rows: int = MAX_TABLE_SAMPLE_ROWS
    table_cell_chars: int = MAX_TABLE_CELL_CHARS
    text_excerpt_chars: int = MAX_TEXT_EXCERPT_CHARS
    reference_bytes: int = MAX_REFERENCE_BYTES
    prompt_bytes: int = MAX_PROMPT_BYTES

    def __post_init__(self) -> None:
        for name in ("table_sample_rows", "table_cell_chars", "text_excerpt_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("reference_bytes", "prompt_bytes"):
            value = getattr(self, name)
            minimum = 2 if name == "reference_bytes" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                qualifier = "at least 2" if name == "reference_bytes" else "positive"
                raise ValueError(f"{name} must be an integer ({qualifier})")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _truncate_string(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    if limit <= 0:
        return "", True
    marker = "...[truncated]"
    if limit <= len(marker):
        return marker[:limit], True
    return value[: limit - len(marker)] + marker, True


def _compact(value: object, *, max_string: int = MAX_TABLE_CELL_CHARS, max_list: int = 24) -> tuple[object, bool]:
    """Compact optional profile/evidence data deterministically."""

    changed = False
    if isinstance(value, str):
        return _truncate_string(value, max_string)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            compacted, item_changed = _compact(item, max_string=max_string, max_list=max_list)
            result[str(key)] = compacted
            changed = changed or item_changed
        return result, changed
    if isinstance(value, (list, tuple)):
        values = list(value)
        if len(values) > max_list:
            values = values[:max_list]
            changed = True
        compacted_items = []
        for item in values:
            compacted, item_changed = _compact(item, max_string=max_string, max_list=max_list)
            compacted_items.append(compacted)
            changed = changed or item_changed
        return compacted_items, changed
    return _json_safe(value), False


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _read_json_artifact(candidate: Mapping[str, Any], key: str, workspace_root: Path) -> dict[str, Any]:
    value = candidate.get(key)
    if not value:
        return {}
    try:
        path = artifact_absolute(str(value), workspace_root)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _int_value(value: object, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _table_columns(candidate: Mapping[str, Any], profile: Mapping[str, Any], metadata: Mapping[str, Any], path: Path) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    original: list[str] = []
    profile_columns = profile.get("columns")
    if isinstance(profile_columns, list):
        for item in profile_columns:
            if isinstance(item, Mapping) and item.get("name") is not None:
                normalized.append(str(item["name"]))
    if not normalized:
        mappings = profile.get("column_mapping") or metadata.get("column_mapping")
        if isinstance(mappings, list):
            for item in mappings:
                if isinstance(item, Mapping) and item.get("normalized") is not None:
                    normalized.append(str(item["normalized"]))
    if not normalized:
        try:
            normalized = [str(name) for name in pl.scan_parquet(path).collect_schema().names()]
        except Exception:
            columns = candidate.get("columns_json") or candidate.get("columns") or []
            if isinstance(columns, str):
                try:
                    columns = json.loads(columns)
                except json.JSONDecodeError:
                    columns = []
            normalized = [str(item) for item in columns] if isinstance(columns, list) else []
    mappings = profile.get("column_mapping") or metadata.get("column_mapping")
    if isinstance(mappings, list):
        by_name = {
            str(item.get("normalized")): item
            for item in mappings
            if isinstance(item, Mapping) and item.get("normalized") is not None
        }
        original = [str(by_name.get(name, {}).get("original", name)) for name in normalized]
    else:
        original = list(normalized)
    return normalized, original


def _truncate_row(row: Mapping[str, object], cell_chars: int) -> tuple[dict[str, object], bool]:
    changed = False
    result: dict[str, object] = {}
    for key, value in row.items():
        safe = _json_safe(value)
        if isinstance(safe, str) and len(safe) > cell_chars:
            result[str(key)], item_changed = _truncate_string(safe, cell_chars)
            changed = changed or item_changed
        else:
            result[str(key)] = safe
    return result, changed


def _sample_table(path: Path, row_count: int, limit: int, cell_chars: int) -> tuple[list[dict[str, object]], bool]:
    if limit < 1:
        return [], row_count > 0
    lazy = pl.scan_parquet(path)
    if row_count <= limit:
        frames = [lazy.head(limit).collect()]
        sampled_all = False
    else:
        head_count = max(1, limit // 3)
        middle_count = max(1, (limit - head_count * 2))
        tail_count = max(1, limit - head_count - middle_count)
        middle_start = max(0, (row_count - middle_count) // 2)
        frames = [
            lazy.head(head_count).collect(),
            lazy.slice(middle_start, middle_count).collect(),
            lazy.tail(tail_count).collect(),
        ]
        sampled_all = True
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    changed = sampled_all
    for frame in frames:
        for row in frame.to_dicts():
            truncated, row_changed = _truncate_row(row, cell_chars)
            key = _dump(truncated)
            if key in seen:
                continue
            seen.add(key)
            rows.append(truncated)
            changed = changed or row_changed
    return rows[:limit], changed


def _table_source_chars(path: Path, columns: Sequence[str]) -> int:
    if not columns:
        return 0
    try:
        expressions = [
            pl.col(name).cast(pl.String, strict=False).str.len_chars().fill_null(0).sum().alias(name)
            for name in columns
        ]
        row = pl.scan_parquet(path).select(expressions).collect().row(0)
        return int(sum(int(value or 0) for value in row))
    except Exception:
        return 0


def _fit_reference(reference: dict[str, object], limits: SemanticInputLimits) -> tuple[dict[str, object], str, bool]:
    initial = _dump(reference)
    if len(initial.encode("utf-8")) <= limits.reference_bytes:
        return reference, initial, False
    compacted, changed = _compact(reference, max_string=limits.table_cell_chars, max_list=24)
    if not isinstance(compacted, dict):
        compacted = {"reference": compacted}
    compacted["input_truncation"] = "deterministic compacting applied"
    rendered = _dump(compacted)
    if len(rendered.encode("utf-8")) > limits.reference_bytes:
        compacted["sample_rows"] = list(compacted.get("sample_rows") or [])[:6]
        compacted["quality_issues"] = list(compacted.get("quality_issues") or [])[:8]
        compacted["profile"] = {
            "columns": list((compacted.get("profile") or {}).get("columns") or [])[:24]
            if isinstance(compacted.get("profile"), Mapping)
            else [],
        }
        rendered = _dump(compacted)
    if len(rendered.encode("utf-8")) > limits.reference_bytes:
        compacted = {
            "asset_id": reference.get("asset_id"),
            "fallback_display_name": reference.get("fallback_display_name"),
            "source_file": reference.get("source_file"),
            "asset_type": reference.get("asset_type"),
            "normalized_columns": list(reference.get("normalized_columns") or [])[:32],
            "sample_rows": list(compacted.get("sample_rows") or [])[:4],
            "input_truncation": "reference data was bounded to the hard byte limit",
        }
        rendered = _dump(compacted)
    if len(rendered.encode("utf-8")) > limits.reference_bytes:
        compacted = {"input_truncation": "reference data was reduced to the hard byte limit"}
        rendered = _dump(compacted)
    if len(rendered.encode("utf-8")) > limits.reference_bytes:
        compacted = {}
        rendered = _dump(compacted)
    if len(rendered.encode("utf-8")) > limits.reference_bytes:
        raise ValueError("semantic reference data cannot fit the configured byte limit")
    return compacted, rendered, bool(changed or rendered != initial)


def _request(
    *,
    candidate: Mapping[str, Any],
    asset_type: str,
    reference_data: dict[str, object],
    source_chars: int,
    sampled_rows: int,
    normalized_artifact_identity: str,
    model: str,
    config_version: str = paths.SEMANTIC_CONFIG_VERSION,
    prompt_version: str | None = None,
    input_truncated: bool,
    limits: SemanticInputLimits,
    validation_columns: Sequence[str] = (),
) -> SemanticRequest:
    version = prompt_version or version_for(asset_type)
    sections = prompt_sections(asset_type)
    bounded_reference, rendered_reference, reference_was_truncated = _fit_reference(reference_data, limits)
    metadata = {
        "source_chars": int(source_chars),
        "sent_chars": len(rendered_reference),
        "sampled_rows": int(sampled_rows),
        "input_truncated": bool(input_truncated or reference_was_truncated),
        "reference_bytes": len(rendered_reference.encode("utf-8")),
        "limits": {
            "reference_bytes": limits.reference_bytes,
            "prompt_bytes": limits.prompt_bytes,
        },
    }
    request = SemanticRequest(
        asset_id=str(candidate["asset_id"]),
        asset_type=asset_type,
        model=model,
        prompt_version=version,
        config_version=config_version,
        normalized_artifact_identity=normalized_artifact_identity,
        instructions=sections["instructions"],
        reference_data=bounded_reference,
        output_contract=sections["output_contract"],
        input_metadata=metadata,
        validation_columns=tuple(validation_columns),
    )
    if request.payload_bytes > limits.prompt_bytes:
        smaller = dict(bounded_reference)
        smaller["sample_rows"] = list(smaller.get("sample_rows") or [])[:2]
        smaller["quality_issues"] = list(smaller.get("quality_issues") or [])[:2]
        smaller["profile"] = {"columns": list((smaller.get("profile") or {}).get("columns") or [])[:8]} if isinstance(smaller.get("profile"), Mapping) else {}
        metadata = dict(metadata)
        metadata["input_truncated"] = True
        smaller_rendered = _dump(smaller)
        metadata["sent_chars"] = len(smaller_rendered)
        metadata["reference_bytes"] = len(smaller_rendered.encode("utf-8"))
        request = SemanticRequest(
            asset_id=request.asset_id,
            asset_type=request.asset_type,
            model=request.model,
            prompt_version=request.prompt_version,
            config_version=request.config_version,
            normalized_artifact_identity=request.normalized_artifact_identity,
            instructions=request.instructions,
            reference_data=smaller,
            output_contract=request.output_contract,
            input_metadata=metadata,
            validation_columns=request.validation_columns,
        )
    if request.payload_bytes > limits.prompt_bytes:
        # The contract itself must fit the configured envelope.  An empty
        # reference is the last safe fallback; never return an over-limit
        # request and hope a provider truncates it differently.
        metadata = dict(request.input_metadata)
        metadata["input_truncated"] = True
        metadata["sent_chars"] = 2
        metadata["reference_bytes"] = 2
        request = SemanticRequest(
            asset_id=request.asset_id,
            asset_type=request.asset_type,
            model=request.model,
            prompt_version=request.prompt_version,
            config_version=request.config_version,
            normalized_artifact_identity=request.normalized_artifact_identity,
            instructions=request.instructions,
            reference_data={},
            output_contract=request.output_contract,
            input_metadata=metadata,
            validation_columns=request.validation_columns,
        )
    if request.payload_bytes > limits.prompt_bytes:
        raise ValueError("semantic prompt contract exceeds the configured prompt byte limit")
    return request


def build_table_request(
    candidate: Mapping[str, Any],
    *,
    workspace_root: Path = paths.WORKSPACE_ROOT,
    model: str = "fake-semantic-v1",
    config_version: str = paths.SEMANTIC_CONFIG_VERSION,
    prompt_version: str | None = None,
    limits: SemanticInputLimits | None = None,
) -> SemanticRequest:
    limits = limits or SemanticInputLimits()
    normalized_value = candidate.get("normalized_artifact_path")
    if not normalized_value:
        raise ValueError("table has no normalized artifact")
    normalized_path = artifact_absolute(str(normalized_value), workspace_root)
    if not normalized_path.is_file():
        raise ValueError("table normalized artifact is missing")
    profile = candidate.get("profile") if isinstance(candidate.get("profile"), Mapping) else _read_json_artifact(candidate, "profile_artifact_path", workspace_root)
    metadata = _read_json_artifact(candidate, "metadata_artifact_path", workspace_root)
    columns, original_columns = _table_columns(candidate, profile, metadata, normalized_path)
    row_count = _int_value(profile.get("row_count"), _int_value(candidate.get("rows")))
    samples, sampled = _sample_table(normalized_path, row_count, limits.table_sample_rows, limits.table_cell_chars)
    source_chars = _table_source_chars(normalized_path, columns)
    reference = {
        "asset_id": str(candidate["asset_id"]),
        "asset_type": "table",
        "fallback_display_name": str(candidate.get("fallback_display_name") or candidate.get("source_file") or candidate["asset_id"]),
        "source_file": str(candidate.get("source_file") or candidate.get("source_relative_path") or ""),
        "source_format": str(candidate.get("source_format") or ""),
        "sheet_or_page": candidate.get("sheet_name") or candidate.get("page_number"),
        "normalized_columns": columns,
        "original_column_names": original_columns,
        "row_count": row_count,
        "column_count": len(columns),
        "profile": profile,
        "quality_issues": candidate.get("quality_issues") or [],
        "provenance": {
            "file_id": candidate.get("file_id"),
            "content_sha256": candidate.get("content_sha256"),
            "source_kind": candidate.get("source_kind") or candidate.get("provenance_kind"),
            "extractor": candidate.get("extractor"),
            "extractor_version": candidate.get("extractor_version"),
            "extraction_run_id": candidate.get("extraction_run_id"),
        },
        "sample_rows": samples,
    }
    return _request(
        candidate=candidate,
        asset_type="table",
        reference_data=reference,
        source_chars=source_chars,
        sampled_rows=len(samples),
        normalized_artifact_identity=sha256_file(normalized_path),
        model=model,
        config_version=config_version,
        prompt_version=prompt_version,
        input_truncated=sampled,
        limits=limits,
        validation_columns=columns,
    )


def _text_windows(path: Path, limit: int) -> tuple[str, bool]:
    size = path.stat().st_size
    if size <= max(1, limit * 4):
        text = path.read_text(encoding="utf-8")
        return text[:limit], len(text) > limit
    window_bytes = max(256, limit * 2 // 3)
    positions = (0, max(0, (size - window_bytes) // 2), max(0, size - window_bytes))
    pieces: list[str] = []
    for position in positions:
        with path.open("rb") as handle:
            handle.seek(position)
            pieces.append(handle.read(window_bytes).decode("utf-8", errors="ignore"))
    separator = "\n...[deterministic excerpt boundary]...\n"
    text = separator.join(pieces)
    return text[:limit], True


def build_text_request(
    candidate: Mapping[str, Any],
    *,
    workspace_root: Path = paths.WORKSPACE_ROOT,
    model: str = "fake-semantic-v1",
    config_version: str = paths.SEMANTIC_CONFIG_VERSION,
    prompt_version: str | None = None,
    limits: SemanticInputLimits | None = None,
    chunks: Sequence[Mapping[str, Any]] | None = None,
) -> SemanticRequest:
    limits = limits or SemanticInputLimits()
    normalized_value = candidate.get("normalized_artifact_path")
    if not normalized_value:
        raise ValueError("text has no normalized artifact")
    normalized_path = artifact_absolute(str(normalized_value), workspace_root)
    if not normalized_path.is_file():
        raise ValueError("text normalized artifact is missing")
    profile = candidate.get("profile") if isinstance(candidate.get("profile"), Mapping) else _read_json_artifact(candidate, "profile_artifact_path", workspace_root)
    excerpt, truncated = _text_windows(normalized_path, limits.text_excerpt_chars)
    source_chars = _int_value(profile.get("char_count"), len(excerpt))
    chunk_values: list[dict[str, object]] = []
    if chunks:
        for item in list(chunks)[:12]:
            if isinstance(item, Mapping):
                chunk_values.append(
                    {
                        "chunk_index": item.get("chunk_index"),
                        "text": str(item.get("text") or "")[: limits.table_cell_chars * 8],
                    }
                )
    reference = {
        "asset_id": str(candidate["asset_id"]),
        "asset_type": "text",
        "fallback_display_name": str(candidate.get("fallback_display_name") or candidate.get("source_file") or candidate["asset_id"]),
        "source_file": str(candidate.get("source_file") or candidate.get("source_relative_path") or ""),
        "source_format": str(candidate.get("source_format") or ""),
        "page_or_section": candidate.get("page_number") or candidate.get("sheet_name"),
        "extraction_source": profile.get("extraction_source") or ("ocr" if "ocr" in str(candidate.get("extractor") or "").casefold() else "native"),
        "profile": profile,
        "quality_issues": candidate.get("quality_issues") or [],
        "provenance": {
            "file_id": candidate.get("file_id"),
            "content_sha256": candidate.get("content_sha256"),
            "source_kind": candidate.get("source_kind") or candidate.get("provenance_kind"),
            "extractor": candidate.get("extractor"),
            "extractor_version": candidate.get("extractor_version"),
            "extraction_run_id": candidate.get("extraction_run_id"),
        },
        "normalized_text_excerpt": excerpt,
        "sampled_chunks": chunk_values,
    }
    return _request(
        candidate=candidate,
        asset_type="text",
        reference_data=reference,
        source_chars=source_chars,
        sampled_rows=0,
        normalized_artifact_identity=sha256_file(normalized_path),
        model=model,
        config_version=config_version,
        prompt_version=prompt_version,
        input_truncated=truncated,
        limits=limits,
    )


def build_semantic_request(candidate: Mapping[str, Any], **kwargs: Any) -> SemanticRequest:
    asset_type = str(candidate.get("asset_type") or "")
    if asset_type == "table":
        return build_table_request(candidate, **kwargs)
    if asset_type == "text":
        return build_text_request(candidate, **kwargs)
    raise ValueError("asset_type must be table or text")
