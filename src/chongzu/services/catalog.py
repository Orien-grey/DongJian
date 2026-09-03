"""Catalog and bounded asset-preview application services."""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import time
from typing import Any, Mapping

import polars as pl

from chongzu import paths
from chongzu.extract.artifacts import artifact_absolute
from chongzu.registry import Registry


MAX_CATALOG_LIMIT = 100
MAX_TABLE_PREVIEW_ROWS = 200
MAX_TEXT_PREVIEW_CHARS = 20_000
MAX_PROFILE_BYTES = 8 * 1024 * 1024


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float):
        # DuckDB/Polars can expose non-JSON finite values in profiles and
        # previews.  Keep the API contract valid without changing artifacts.
        if value != value or value in {float("inf"), float("-inf")}:  # noqa: PLR0124
            return None
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _parse_json_file(path: Path, *, max_bytes: int = MAX_PROFILE_BYTES) -> Any:
    if not path.is_file() or path.stat().st_size > max_bytes:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _asset_type_id(row: Mapping[str, Any]) -> str:
    return str(row.get("asset_id") or "")


class CatalogService:
    """Read-only catalog facade with safe artifact resolution."""

    def __init__(
        self,
        *,
        registry_path: Path | str | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.registry_path = Path(registry_path or paths.REGISTRY_PATH).resolve()
        self.workspace_root = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()

    def _open(self) -> Registry:
        return Registry.open_reader(self.registry_path)

    @staticmethod
    def _catalog_item(row: Mapping[str, Any]) -> dict[str, Any]:
        asset_type = str(row.get("asset_type") or "")
        return _json_value(
            {
                "assetId": row.get("asset_id"),
                "assetType": asset_type,
                "fallbackDisplayName": row.get("fallback_display_name"),
                "semanticDisplayName": row.get("semantic_display_name"),
                "effectiveDisplayName": row.get("effective_display_name"),
                "source": {
                    "fileId": row.get("file_id"),
                    "relativePath": row.get("source_file"),
                    "format": row.get("source_format"),
                    "root": row.get("source_root"),
                    "sha256": row.get("content_sha256"),
                },
                "extractor": row.get("extractor"),
                "extractorVersion": row.get("extractor_version"),
                "sourceKind": row.get("source_kind"),
                "sheetName": row.get("sheet_name"),
                "pageNumber": row.get("page_number"),
                "rows": row.get("rows"),
                "columns": row.get("columns"),
                "chars": row.get("chars"),
                "chunks": row.get("chunks"),
                "qualityStatus": row.get("quality_status"),
                "qualityIssueCount": row.get("quality_issue_count", 0),
                "semanticStatus": row.get("semantic_status"),
                "semanticModel": row.get("semantic_model"),
                "semanticConfidence": row.get("semantic_confidence"),
                "cleaningStatus": row.get("cleaning_status"),
                "cleaningRunId": row.get("cleaning_run_id"),
                "provenanceKind": row.get("provenance_kind"),
                "createdAt": row.get("created_at"),
            }
        )

    def overview(self) -> dict[str, Any]:
        opened = time.perf_counter_ns()
        registry = self._open()
        registry_open_ms = (time.perf_counter_ns() - opened) / 1_000_000
        try:
            queried = time.perf_counter_ns()
            summary = registry.catalog_summary()
            _processed, failed, deferred, _ = registry.processing_counts()
            format_cursor = registry.connection.execute(
                "SELECT source_format, COUNT(*) FROM catalog_assets GROUP BY source_format ORDER BY source_format"
            )
            formats = {str(fmt or "unknown"): int(count) for fmt, count in format_cursor.fetchall()}
            query_ms = (time.perf_counter_ns() - queried) / 1_000_000
            materialized = {
                "files": int(summary.get("files", 0)),
                "supported": int(summary.get("supported_files", 0)),
                "unsupported": int(summary.get("unsupported_files", 0)),
                "failed": int(failed),
                "deferred": int(deferred),
                "tableAssets": int(summary.get("table_assets", 0)),
                "textAssets": int(summary.get("text_assets", 0)),
                "textChunks": int(summary.get("text_chunks", 0)),
                "ready": int(summary.get("ready", 0)),
                "needsReview": int(summary.get("needs_review", 0)),
                "unusable": int(summary.get("unusable", 0)),
                "qualityIssues": int(summary.get("quality_issues", 0)),
                "openQualityIssues": int(summary.get("open_quality_issues", 0)),
                "semanticPending": int(summary.get("semantic_pending", 0)),
                "semanticEnriched": int(summary.get("semantic_enriched", 0)),
                "formats": formats,
            }
            materialize_started = time.perf_counter_ns()
            result = _json_value(materialized)
            materialize_ms = (time.perf_counter_ns() - materialize_started) / 1_000_000
            serialized_started = time.perf_counter_ns()
            json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            serialize_ms = (time.perf_counter_ns() - serialized_started) / 1_000_000
            result["timings"] = {
                "registry_open_ms": round(registry_open_ms, 3),
                "query_ms": round(query_ms, 3),
                "materialize_ms": round(materialize_ms, 3),
                "serialize_ms": round(serialize_ms, 3),
            }
            return result
        finally:
            registry.close()

    def list_assets(
        self,
        *,
        asset_type: str | None = None,
        quality_status: str | None = None,
        source_format: str | None = None,
        query: str | None = None,
        limit: int = MAX_CATALOG_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        if limit < 1 or limit > MAX_CATALOG_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_CATALOG_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if asset_type not in {None, "table", "text"}:
            raise ValueError("type must be table or text")
        if quality_status not in {None, "ready", "needs_review", "unusable"}:
            raise ValueError("quality must be ready, needs_review, or unusable")
        opened = time.perf_counter_ns()
        registry = self._open()
        registry_open_ms = (time.perf_counter_ns() - opened) / 1_000_000
        try:
            queried = time.perf_counter_ns()
            rows = registry.list_catalog_assets(
                asset_type=asset_type,
                quality_status=quality_status,
                source_format=source_format,
                query=query,
                limit=limit,
                offset=offset,
                include_total=True,
            )
            if rows:
                total = int(rows[0].pop("_catalog_total", 0) or 0)
                for row in rows[1:]:
                    row.pop("_catalog_total", None)
            else:
                total = registry.count_catalog_assets(
                    asset_type=asset_type,
                    quality_status=quality_status,
                    source_format=source_format,
                    query=query,
                )
            quality_counts = registry.visible_quality_issue_counts(rows)
            for row in rows:
                row["quality_issue_count"] = quality_counts.get(str(row.get("asset_id") or ""), 0)
            query_ms = (time.perf_counter_ns() - queried) / 1_000_000
            materialize_started = time.perf_counter_ns()
            items = [self._catalog_item(row) for row in rows]
            materialize_ms = (time.perf_counter_ns() - materialize_started) / 1_000_000
            result = {
                "items": items,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "total": total,
                    "hasNext": offset + len(rows) < total,
                },
            }
            serialized_started = time.perf_counter_ns()
            json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            serialize_ms = (time.perf_counter_ns() - serialized_started) / 1_000_000
            result["timings"] = {
                "registry_open_ms": round(registry_open_ms, 3),
                "query_ms": round(query_ms, 3),
                "materialize_ms": round(materialize_ms, 3),
                "serialize_ms": round(serialize_ms, 3),
            }
            return result
        finally:
            registry.close()

    def _raw_detail(self, asset_id: str) -> dict[str, Any] | None:
        registry = self._open()
        try:
            row = registry.catalog_asset_details(asset_id)
            if row is None:
                return None
            semantic_cursor = registry.connection.execute(
                """
                SELECT display_name, category, description, keywords_json, summary,
                       semantic_fields_json, model, prompt_version, confidence,
                       generated_at, semantic_run_id, input_hash
                FROM semantic_metadata
                WHERE asset_id=? AND current=TRUE
                ORDER BY generated_at DESC, semantic_run_id DESC NULLS LAST
                LIMIT 1
                """,
                [asset_id],
            )
            semantic = semantic_cursor.fetchone()
            semantic_columns = [item[0] for item in semantic_cursor.description] if semantic else []
            semantic_row = dict(zip(semantic_columns, semantic)) if semantic else None
            return {"catalog": dict(row), "semantic": semantic_row}
        finally:
            registry.close()

    def asset_detail(self, asset_id: str) -> dict[str, Any] | None:
        result = self._raw_detail(asset_id)
        if result is None:
            return None
        row = result["catalog"]
        asset_type = str(row.get("asset_type") or "")
        metadata_path_value = row.get("metadata_artifact_path")
        metadata = None
        if metadata_path_value:
            try:
                metadata = _parse_json_file(artifact_absolute(str(metadata_path_value), self.workspace_root))
            except (OSError, ValueError):
                metadata = None
        semantic = result["semantic"]
        if semantic:
            parsed_keywords = semantic.get("keywords_json")
            parsed_fields = semantic.get("semantic_fields_json")
            for key, value in (("keywords", parsed_keywords), ("semanticFields", parsed_fields)):
                if isinstance(value, str):
                    try:
                        semantic[key] = json.loads(value)
                    except json.JSONDecodeError:
                        semantic[key] = value
            semantic.pop("keywords_json", None)
            semantic.pop("semantic_fields_json", None)
        detail = {
            "assetId": row.get("asset_id"),
            "assetType": asset_type,
            "displayName": row.get("effective_display_name"),
            "fallbackDisplayName": row.get("fallback_display_name"),
            "semanticDisplayName": row.get("semantic_display_name"),
            "qualityStatus": row.get("quality_status"),
            "qualityIssueCount": row.get("quality_issue_count", 0),
            "cleaningStatus": row.get("cleaning_status"),
            "semanticStatus": row.get("semantic_status"),
            "source": {
                "fileId": row.get("file_id"),
                "root": row.get("source_root"),
                "relativePath": row.get("source_file"),
                "format": row.get("source_format"),
                "sha256": row.get("content_sha256"),
            },
            "provenance": {
                "extractor": row.get("extractor"),
                "extractorVersion": row.get("extractor_version"),
                "sourceKind": row.get("source_kind"),
                "sheetName": row.get("sheet_name"),
                "pageNumber": row.get("page_number"),
                "extractionRunId": row.get("extraction_run_id"),
                "sourceRange": (metadata or {}).get("source_range") if isinstance(metadata, Mapping) else None,
                "bbox": (metadata or {}).get("bbox") if isinstance(metadata, Mapping) else None,
                "renderMetadata": (metadata or {}).get("render_metadata") if isinstance(metadata, Mapping) else None,
                "provider": (metadata or {}).get("provider") if isinstance(metadata, Mapping) else None,
                "providerContract": (metadata or {}).get("provider_contract") if isinstance(metadata, Mapping) else None,
                "model": (metadata or {}).get("model") if isinstance(metadata, Mapping) else None,
            },
            "artifacts": {
                "raw": row.get("raw_artifact_path"),
                "normalized": row.get("normalized_artifact_path"),
                "extractionNormalized": row.get("extraction_normalized_artifact_path"),
                "metadata": row.get("metadata_artifact_path"),
                "cleaningManifest": row.get("cleaning_manifest_path"),
                "profile": row.get("profile_artifact_path"),
            },
            "dimensions": {
                "rows": row.get("rows"),
                "columns": row.get("columns"),
                "chars": row.get("chars"),
                "chunks": row.get("chunks"),
            },
            "extractorMetadata": metadata,
            "profile": row.get("profile"),
            "qualityIssues": [],
            "semantic": semantic,
            "semanticHistory": row.get("semantic_history") or [],
            "createdAt": row.get("created_at"),
        }
        registry = self._open()
        try:
            quality_rows = registry.list_quality_issues(asset_id=asset_id, limit=100_000)
        finally:
            registry.close()
        detail["qualityIssues"] = quality_rows
        return _json_value(detail)

    def _asset_row(self, asset_id: str, expected_type: str) -> dict[str, Any]:
        registry = self._open()
        try:
            row = registry.catalog_asset_details(asset_id)
        finally:
            registry.close()
        if row is None or str(row.get("asset_type")) != expected_type:
            raise KeyError(asset_id)
        return row

    def _artifact_path(self, row: Mapping[str, Any], layer: str) -> Path:
        if layer not in {"raw", "normalized"}:
            raise ValueError("layer must be raw or normalized")
        key = "raw_artifact_path" if layer == "raw" else "normalized_artifact_path"
        value = row.get(key)
        if not value:
            raise FileNotFoundError(f"{layer} artifact is unavailable")
        return artifact_absolute(str(value), self.workspace_root)

    def table_preview(
        self,
        asset_id: str,
        *,
        layer: str = "normalized",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if limit < 1 or limit > MAX_TABLE_PREVIEW_ROWS:
            raise ValueError(f"limit must be between 1 and {MAX_TABLE_PREVIEW_ROWS}")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        row = self._asset_row(asset_id, "table")
        path = self._artifact_path(row, layer)
        if not path.is_file():
            raise FileNotFoundError(f"{layer} artifact is unavailable")
        frame = pl.scan_parquet(str(path)).slice(offset, limit).collect()
        records = frame.to_dicts()
        total = row.get("rows")
        if layer == "raw":
            try:
                total = int(pl.scan_parquet(str(path)).select(pl.len()).collect().item())
            except Exception:  # pragma: no cover - defensive for a damaged preview artifact
                total = None
        return _json_value(
            {
                "assetId": asset_id,
                "assetType": "table",
                "layer": layer,
                "columns": frame.columns,
                "rows": records,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "total": total,
                    "hasNext": len(records) == limit,
                },
            }
        )

    @staticmethod
    def _read_text_window(path: Path, offset: int, limit: int) -> tuple[str, bool]:
        remaining = offset
        pieces: list[str] = []
        collected = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            while True:
                block = handle.read(64 * 1024)
                if not block:
                    break
                if remaining >= len(block):
                    remaining -= len(block)
                    continue
                block = block[remaining:]
                remaining = 0
                take = min(limit - collected, len(block))
                if take:
                    pieces.append(block[:take])
                    collected += take
                if collected >= limit:
                    return "".join(pieces), bool(block[take:]) or bool(handle.read(1))
        return "".join(pieces), False

    def text_preview(self, asset_id: str, *, offset: int = 0, limit: int = 8_000) -> dict[str, Any]:
        if limit < 1 or limit > MAX_TEXT_PREVIEW_CHARS:
            raise ValueError(f"limit must be between 1 and {MAX_TEXT_PREVIEW_CHARS}")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        row = self._asset_row(asset_id, "text")
        value = row.get("normalized_artifact_path")
        layer = "normalized"
        if not value:
            value = row.get("raw_artifact_path")
            layer = "raw"
        if not value:
            raise FileNotFoundError("text artifact is unavailable")
        path = artifact_absolute(str(value), self.workspace_root)
        if not path.is_file():
            raise FileNotFoundError("text artifact is unavailable")
        text, has_next = self._read_text_window(path, offset, limit)
        return _json_value(
            {
                "assetId": asset_id,
                "assetType": "text",
                "layer": layer,
                "text": text,
                "offset": offset,
                "limit": limit,
                "hasNext": has_next,
                "chars": row.get("chars"),
                "chunks": row.get("chunks"),
            }
        )
