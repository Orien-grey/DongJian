"""Deterministic text normalization that never rewrites extraction meaning."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from typing import Any, Mapping

from chongzu import paths
from chongzu.extract.artifacts import artifact_absolute, workspace_relative, write_json_atomic
from chongzu.extract.pdf.artifacts import write_text_atomic

from .models import CleaningAssetResult
from .profiling import profile_text


CLEANER_NAME = "deterministic-text-cleaner"
CLEANER_VERSION = paths.TEXT_CLEANER_VERSION


def normalize_text_value(value: str) -> tuple[str, list[dict[str, Any]]]:
    """Apply only Unicode/control/newline/whitespace normalization."""

    actions: list[dict[str, Any]] = []
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        actions.append({"type": "unicode_nfc", "count": 1})
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    trimmed_lines = [line.rstrip(" \t") for line in lines]
    trailing_changes = sum(before != after for before, after in zip(lines, trimmed_lines, strict=True))
    if trailing_changes:
        actions.append({"type": "trim_trailing_whitespace", "count": trailing_changes})
    normalized = "\n".join(trimmed_lines)
    compressed = re.sub(r"\n{3,}", "\n\n", normalized)
    if compressed != normalized:
        actions.append({"type": "compress_excessive_blank_lines", "count": normalized.count("\n") - compressed.count("\n")})
    normalized = compressed
    control_removed = sum(
        1
        for char in normalized
        if (ord(char) < 32 and char not in {"\n", "\t"}) or 127 <= ord(char) <= 159
    )
    if control_removed:
        normalized = "".join(
            char
            for char in normalized
            if not ((ord(char) < 32 and char not in {"\n", "\t"}) or 127 <= ord(char) <= 159)
        )
        actions.append({"type": "remove_control_characters", "count": control_removed})
    return normalized, actions


def _profile_id(cleaning_identity: str, cleaning_run_id: str) -> str:
    return f"xprof_{hashlib.sha256((cleaning_identity + ':' + cleaning_run_id + ':profile').encode('utf-8')).hexdigest()[:32]}"


def clean_text(
    candidate: Mapping[str, Any],
    *,
    workspace_root: Path,
    raw_artifact_identity: str,
    cleaning_identity: str,
    cleaning_run_id: str,
) -> CleaningAssetResult:
    result = CleaningAssetResult(
        asset_id=str(candidate["asset_id"]),
        asset_type="text",
        file_id=str(candidate["file_id"]),
        content_sha256=str(candidate["content_sha256"]),
        source_root=str(candidate["source_root"]),
        source_relative_path=str(candidate["source_relative_path"] or ""),
        raw_artifact_identity=raw_artifact_identity,
        cleaner=CLEANER_NAME,
        cleaner_version=paths.TEXT_CLEANER_VERSION,
        config_version=paths.CLEANING_CONFIG_VERSION,
        cleaning_run_id=cleaning_run_id,
        cleaning_identity=cleaning_identity,
    )
    try:
        raw_path = artifact_absolute(str(candidate["raw_artifact_path"]), workspace_root)
        metadata_path = artifact_absolute(str(candidate["metadata_artifact_path"]), workspace_root)
        raw_text = raw_path.read_text(encoding="utf-8")
        normalize_started = time.perf_counter_ns()
        normalized, actions = normalize_text_value(raw_text)
        result.timings.normalize_ms = (time.perf_counter_ns() - normalize_started) / 1_000_000
        metadata: dict[str, Any] = {}
        if metadata_path.is_file():
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                metadata = payload
        if isinstance(metadata.get("quality_warnings"), list):
            result.warnings = [{"type": str(item)} for item in metadata["quality_warnings"]]
        profile_id = _profile_id(cleaning_identity, cleaning_run_id)
        profile_started = time.perf_counter_ns()
        profile = profile_text(
            normalized,
            candidate,
            profile_id=profile_id,
            cleaning_identity=cleaning_identity,
            metadata=metadata,
            chunk_count=int(candidate.get("chunk_count") or 0),
        )
        result.timings.profile_ms = (time.perf_counter_ns() - profile_started) / 1_000_000
        profile["cleaning_actions"] = actions
        target_dir = workspace_root / "artifacts" / "cleaning" / "text" / str(candidate["asset_id"]) / cleaning_identity[:16]
        normalized_path = target_dir / "normalized.txt"
        manifest_path = target_dir / "cleaning.json"
        profile_path = target_dir / "profile.json"
        started = time.perf_counter_ns()
        write_text_atomic(normalized_path, normalized)
        write_json_atomic(
            manifest_path,
            {
                "cleaning_version": paths.TEXT_CLEANER_VERSION,
                "config_version": paths.CLEANING_CONFIG_VERSION,
                "profile_version": paths.PROFILE_CONFIG_VERSION,
                "cleaning_identity": cleaning_identity,
                "cleaning_run_id": cleaning_run_id,
                "asset_id": str(candidate["asset_id"]),
                "asset_type": "text",
                "file_id": str(candidate["file_id"]),
                "content_sha256": str(candidate["content_sha256"]),
                "raw_artifact_identity": raw_artifact_identity,
                "raw_artifact_path": str(candidate["raw_artifact_path"]),
                "normalized_artifact_path": workspace_relative(normalized_path, workspace_root),
                "actions": actions,
                "layers": {
                    "raw": str(candidate["raw_artifact_path"]),
                    "normalized": "normalized.txt",
                    "semantic": None,
                },
            },
        )
        write_json_atomic(profile_path, profile)
        result.timings.artifact_write_ms = (time.perf_counter_ns() - started) / 1_000_000
        result.normalized_artifact_path = workspace_relative(normalized_path, workspace_root)
        result.manifest_artifact_path = workspace_relative(manifest_path, workspace_root)
        result.profile_artifact_path = workspace_relative(profile_path, workspace_root)
        result.profile = profile
        result.profile_row = {
            "profile_id": profile_id,
            "text_asset_id": str(candidate["asset_id"]),
            "content_sha256": result.content_sha256,
            "char_count": int(profile["char_count"]),
            "line_count": int(profile["line_count"]),
            "page_count": int(profile["page_count"]),
            "block_count": int(profile["block_count"]),
            "chunk_count": int(profile["chunk_count"]),
            "language_hint": profile.get("language_hint"),
            "extraction_source": profile["extraction_source"],
            "ocr_mean_confidence": profile.get("ocr_mean_confidence"),
            "empty_content": bool(profile["empty_content"]),
            "low_content": bool(profile["low_content"]),
        }
    except Exception as exc:  # isolate one asset
        result.status = "failed"
        result.error_category = "text_cleaning_error"
        result.error_message = str(exc)
    return result
