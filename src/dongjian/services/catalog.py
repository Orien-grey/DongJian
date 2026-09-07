"""Catalog and bounded asset-preview application services."""

from __future__ import annotations

import copy
from datetime import date, datetime
import json
import mimetypes
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping
from urllib.parse import quote
from uuid import uuid4

import polars as pl

from dongjian import paths
from dongjian.extract.artifacts import artifact_absolute
from dongjian.registry import Registry


MAX_CATALOG_LIMIT = 100
MAX_TABLE_PREVIEW_ROWS = 200
MAX_TEXT_PREVIEW_CHARS = 20_000
MAX_PROFILE_BYTES = 8 * 1024 * 1024
MAX_FILE_TEXT_PREVIEW_CHARS = 12_000
MAX_FILE_CHILD_ASSETS = 500
MAX_FILE_CONTENT_TABLES = 200
MAX_FILE_CONTENT_TABLE_ROWS = 20
MAX_FILE_CONTENT_TABLE_COLUMNS = 32
MAX_FILE_CONTENT_CELL_CHARS = 2_048
MAX_FILE_CONTENT_BLOCKS = 500
MAX_SOURCE_PREVIEW_BYTES = 16 * 1024 * 1024
MAX_SOURCE_PREVIEW_DIMENSION = 1_800


def _content_bbox(value: object) -> tuple[float, float, float, float] | None:
    """Read the two bbox spellings used by native PDF and OCR artifacts."""

    if not isinstance(value, Mapping):
        return None
    keys = ("x0", "y0", "x1", "y1")
    if all(key in value for key in keys):
        raw = [value[key] for key in keys]
    elif all(key in value for key in ("x1", "y1", "x2", "y2")):
        raw = [value["x1"], value["y1"], value["x2"], value["y2"]]
    else:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in raw)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _bbox_overlap_ratio(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> float:
    left = max(inner[0], outer[0])
    top = max(inner[1], outer[1])
    right = min(inner[2], outer[2])
    bottom = min(inner[3], outer[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area = max(0.0, inner[2] - inner[0]) * max(0.0, inner[3] - inner[1])
    return intersection / area if area else 0.0


def _presentation_text(value: str) -> str:
    """Keep source line breaks while joining orphaned list markers safely."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    markers = {"•", "·", "▪", "◦", "-", "–", "—"}
    result: list[str] = []
    index = 0
    while index < len(lines):
        current = lines[index].strip()
        if current and (current in markers or (len(current) <= 4 and current[:-1].isdigit() and current[-1:] in {".", ")", "、"})):
            next_index = index + 1
            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            if next_index < len(lines):
                result.append(f"{current} {lines[next_index].strip()}".strip())
                index = next_index + 1
                continue
        result.append(lines[index])
        index += 1
    return "\n".join(result).strip()


def _join_spatial_text(items: list[Mapping[str, Any]]) -> str:
    """Join adjacent native/OCR fragments into conservative reading lines.

    Extraction metadata only gives reliable rectangles, not document semantics.
    Joining fragments that share a baseline keeps word-level OCR/PDF output
    readable while leaving different rows/columns as separate lines.
    """

    lines: list[list[str]] = []
    previous_box: tuple[float, float, float, float] | None = None
    for item in items:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        box = item.get("bbox")
        same_line = False
        if isinstance(box, tuple) and previous_box is not None:
            previous_height = max(1.0, previous_box[3] - previous_box[1])
            current_height = max(1.0, box[3] - box[1])
            same_line = (
                abs(box[1] - previous_box[1]) <= max(3.0, min(previous_height, current_height) * 0.5)
                and box[0] >= previous_box[0] - 2.0
            )
        if same_line and lines:
            lines[-1].append(text)
        else:
            lines.append([text])
        previous_box = box if isinstance(box, tuple) else None
    return _presentation_text("\n".join(" ".join(line) for line in lines))


# The catalog view is intentionally rich for the catalog/search surfaces, but
# its global latest-cleaning/semantic/chunk CTEs are unnecessary for one file.
# File detail uses this file-keyed equivalent so a detail request does not
# first materialize unrelated assets.  Keep the projection aligned with
# ``catalog_assets`` because the existing asset/detail serializers consume the
# same row shape.
FILE_ASSET_ROWS_QUERY = """
WITH file_scope AS (
    SELECT * FROM files
    WHERE file_id=? AND current_presence_state='present'
), asset_keys AS (
    SELECT t.table_id AS asset_id, 'table' AS asset_type, t.content_sha256
    FROM table_assets t JOIN file_scope f ON f.file_id=t.file_id
    WHERE t.is_current=TRUE
    UNION ALL
    SELECT t.text_asset_id AS asset_id, 'text' AS asset_type, t.content_sha256
    FROM text_assets t JOIN file_scope f ON f.file_id=t.file_id
    WHERE t.is_current=TRUE
), latest_cleaning AS (
    SELECT * EXCLUDE (row_number)
    FROM (
        SELECT c.*,
               ROW_NUMBER() OVER (
                   PARTITION BY c.asset_id, c.content_sha256
                   ORDER BY c.finished_at DESC NULLS LAST, c.started_at DESC, c.cleaning_run_id DESC
               ) AS row_number
        FROM cleaning_runs c
        JOIN asset_keys k ON k.asset_id=c.asset_id AND k.content_sha256=c.content_sha256
    ) ranked
    WHERE row_number=1
), issue_counts AS (
    SELECT q.asset_id,
           COUNT(*) FILTER (WHERE q.status IN ('open','accepted')) AS issue_count
    FROM (
        SELECT q.*, ROW_NUMBER() OVER (
            PARTITION BY q.asset_id, q.issue_type
            ORDER BY q.created_at DESC NULLS LAST, q.issue_id
        ) AS issue_rank
        FROM quality_issues q
        WHERE q.issue_type <> 'possible_table_candidate'
    ) q
    JOIN asset_keys k ON k.asset_id=q.asset_id
    LEFT JOIN latest_cleaning c ON c.cleaning_run_id=q.cleaning_run_id
    WHERE q.issue_rank=1
      AND (q.cleaning_run_id IS NULL OR c.cleaning_run_id IS NOT NULL)
    GROUP BY q.asset_id
), semantic_current AS (
    SELECT * EXCLUDE (row_number)
    FROM (
        SELECT s.*,
               ROW_NUMBER() OVER (
                   PARTITION BY s.asset_id, s.asset_type
                   ORDER BY s.current DESC, s.generated_at DESC, s.semantic_run_id DESC NULLS LAST
               ) AS row_number
        FROM semantic_metadata s
        JOIN asset_keys k ON k.asset_id=s.asset_id AND k.asset_type=s.asset_type
        WHERE s.current=TRUE
    ) ranked
    WHERE row_number=1
), text_chunk_counts AS (
    SELECT c.text_asset_id, COUNT(*) AS chunk_count
    FROM text_chunks c
    JOIN asset_keys k ON k.asset_id=c.text_asset_id AND k.asset_type='text'
    GROUP BY c.text_asset_id
), table_catalog AS (
    SELECT
        t.table_id AS asset_id,
        'table' AS asset_type,
        t.file_id,
        f.source_root,
        t.content_sha256,
        t.source_relative_path AS source_file,
        f.business_format AS source_format,
        t.extractor,
        t.extractor_version,
        t.source_kind,
        t.sheet_name,
        t.page_number,
        CASE
            WHEN t.sheet_name IS NOT NULL THEN f.filename || ' / ' || t.sheet_name
            WHEN t.page_number IS NOT NULL THEN f.filename || ' / Page ' || CAST(t.page_number AS VARCHAR) || ' / Table ' || CAST(ROW_NUMBER() OVER (PARTITION BY t.file_id, t.page_number ORDER BY t.table_id) AS VARCHAR)
            WHEN t.source_kind='image' THEN f.filename || ' / Table ' || CAST(ROW_NUMBER() OVER (PARTITION BY t.file_id ORDER BY t.table_id) AS VARCHAR)
            ELSE f.filename
        END AS fallback_display_name,
        s.display_name AS semantic_display_name,
        COALESCE(s.display_name,
            CASE
                WHEN t.sheet_name IS NOT NULL THEN f.filename || ' / ' || t.sheet_name
                WHEN t.page_number IS NOT NULL THEN f.filename || ' / Page ' || CAST(t.page_number AS VARCHAR) || ' / Table ' || CAST(ROW_NUMBER() OVER (PARTITION BY t.file_id, t.page_number ORDER BY t.table_id) AS VARCHAR)
                WHEN t.source_kind='image' THEN f.filename || ' / Table ' || CAST(ROW_NUMBER() OVER (PARTITION BY t.file_id ORDER BY t.table_id) AS VARCHAR)
                ELSE f.filename
            END) AS effective_display_name,
        s.category,
        COALESCE(p.row_count, t.row_count) AS "rows",
        CAST(NULL AS BIGINT) AS "chars",
        COALESCE(p.column_count, t.column_count) AS "columns",
        CAST(NULL AS BIGINT) AS chunks,
        COALESCE(c.quality_status,
            CASE
                WHEN f.business_format='pdf' OR t.source_kind IN ('page','image') OR LOWER(t.extractor) LIKE '%img2table%' THEN 'needs_review'
                WHEN t.quality_status='pass' THEN 'ready'
                WHEN t.quality_status='fail' THEN 'unusable'
                ELSE 'needs_review'
            END) AS quality_status,
        COALESCE(i.issue_count, 0) AS quality_issue_count,
        CASE WHEN s.asset_id IS NULL THEN 'pending' ELSE 'enriched' END AS semantic_status,
        s.model AS semantic_model,
        s.confidence AS semantic_confidence,
        s.semantic_run_id,
        COALESCE(c.status, 'not_run') AS cleaning_status,
        c.cleaning_run_id,
        c.cleaning_identity,
        c.cleaner,
        c.cleaner_version,
        t.raw_artifact_path,
        COALESCE(c.normalized_artifact_path, t.normalized_artifact_path) AS normalized_artifact_path,
        c.normalized_artifact_path AS cleaned_normalized_artifact_path,
        t.normalized_artifact_path AS extraction_normalized_artifact_path,
        t.metadata_artifact_path,
        c.manifest_artifact_path AS cleaning_manifest_path,
        c.profile_artifact_path,
        t.source_kind AS provenance_kind,
        t.extraction_run_id,
        t.created_at
    FROM table_assets t
    JOIN file_scope f ON f.file_id=t.file_id
    LEFT JOIN latest_cleaning c ON c.asset_id=t.table_id AND c.content_sha256=t.content_sha256
    LEFT JOIN table_profiles p ON p.cleaning_run_id=c.cleaning_run_id AND p.table_id=t.table_id
    LEFT JOIN issue_counts i ON i.asset_id=t.table_id
    LEFT JOIN semantic_current s ON s.asset_id=t.table_id AND s.asset_type='table'
    WHERE t.is_current=TRUE
), text_catalog AS (
    SELECT
        t.text_asset_id AS asset_id,
        'text' AS asset_type,
        t.file_id,
        f.source_root,
        t.content_sha256,
        t.source_relative_path AS source_file,
        f.business_format AS source_format,
        t.extractor,
        t.extractor_version,
        t.source_kind,
        t.section AS sheet_name,
        t.page_number,
        CASE
            WHEN t.page_number IS NOT NULL THEN f.filename || ' / Page ' || CAST(t.page_number AS VARCHAR)
            ELSE f.filename
        END AS fallback_display_name,
        s.display_name AS semantic_display_name,
        COALESCE(s.display_name,
            CASE
                WHEN t.page_number IS NOT NULL THEN f.filename || ' / Page ' || CAST(t.page_number AS VARCHAR)
                ELSE f.filename
            END) AS effective_display_name,
        s.category,
        CAST(NULL AS BIGINT) AS "rows",
        COALESCE(p.char_count, LENGTH(t.text)) AS "chars",
        CAST(NULL AS BIGINT) AS "columns",
        COALESCE(p.chunk_count, tc.chunk_count, 0) AS chunks,
        COALESCE(c.quality_status,
            CASE
                WHEN LENGTH(TRIM(t.text))=0 THEN 'unusable'
                WHEN LOWER(t.extractor) LIKE '%ocr%' THEN 'needs_review'
                ELSE 'ready'
            END) AS quality_status,
        COALESCE(i.issue_count, 0) AS quality_issue_count,
        CASE WHEN s.asset_id IS NULL THEN 'pending' ELSE 'enriched' END AS semantic_status,
        s.model AS semantic_model,
        s.confidence AS semantic_confidence,
        s.semantic_run_id,
        COALESCE(c.status, 'not_run') AS cleaning_status,
        c.cleaning_run_id,
        c.cleaning_identity,
        c.cleaner,
        c.cleaner_version,
        t.raw_artifact_path,
        COALESCE(c.normalized_artifact_path, t.normalized_artifact_path) AS normalized_artifact_path,
        c.normalized_artifact_path AS cleaned_normalized_artifact_path,
        t.normalized_artifact_path AS extraction_normalized_artifact_path,
        t.metadata_artifact_path,
        c.manifest_artifact_path AS cleaning_manifest_path,
        c.profile_artifact_path,
        t.source_kind AS provenance_kind,
        t.extraction_run_id,
        t.created_at
    FROM text_assets t
    JOIN file_scope f ON f.file_id=t.file_id
    LEFT JOIN latest_cleaning c ON c.asset_id=t.text_asset_id AND c.content_sha256=t.content_sha256
    LEFT JOIN text_profiles p ON p.cleaning_run_id=c.cleaning_run_id AND p.text_asset_id=t.text_asset_id
    LEFT JOIN text_chunk_counts tc ON tc.text_asset_id=t.text_asset_id
    LEFT JOIN issue_counts i ON i.asset_id=t.text_asset_id
    LEFT JOIN semantic_current s ON s.asset_id=t.text_asset_id AND s.asset_type='text'
    WHERE t.is_current=TRUE
)
SELECT * FROM table_catalog
UNION ALL
SELECT * FROM text_catalog
"""


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
        self._content_cache: dict[tuple[str, int | None, str | None], dict[str, Any]] = {}
        self._content_cache_lock = threading.RLock()
        self._pdf_page_counts: dict[str, int] = {}

    def invalidate_file(self, file_id: str) -> None:
        """Drop every cached content locator for one file."""

        normalized = str(file_id)
        with self._content_cache_lock:
            for key in [item for item in self._content_cache if item[0] == normalized]:
                self._content_cache.pop(key, None)

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
                "SELECT business_format, COUNT(*) FROM files WHERE current_presence_state='present' GROUP BY business_format ORDER BY business_format"
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

    @staticmethod
    def _file_item(row: Mapping[str, Any]) -> dict[str, Any]:
        return _json_value(
            {
                "fileId": row.get("file_id"),
                "displayName": row.get("filename") or row.get("relative_path"),
                "relativePath": row.get("relative_path"),
                "format": row.get("business_format") or row.get("observed_extension", "").lstrip("."),
                "observedExtension": row.get("observed_extension"),
                "sizeBytes": row.get("size_bytes"),
                "sha256": row.get("sha256"),
                "supportStatus": row.get("support_status"),
                "processingStatus": row.get("processing_status", "not_processed"),
                "evidenceStatus": row.get("evidence_status", "not_assessed"),
                "fileInsightStatus": row.get("file_insight_status", "not_started"),
                "textAssets": int(row.get("text_assets") or 0),
                "textPages": int(row.get("text_pages") or 0),
                "tableAssets": int(row.get("table_assets") or 0),
                "qualityStatus": row.get("quality_status", "not_assessed"),
                "qualityIssueCount": row.get("quality_issue_count", 0),
                "semanticStatus": row.get("semantic_status", "pending"),
                "sourceRoot": row.get("source_root"),
                "latestErrorCode": row.get("latest_error_code"),
                "latestErrorMessage": row.get("latest_error_message"),
            }
        )

    @staticmethod
    def _file_category_clause(category: str | None) -> str | None:
        if category is None or category in {"", "all"}:
            return None
        if category == "table_file":
            return "COALESCE(f.table_assets, 0) > 0"
        if category == "document":
            return "f.business_format IN ('pdf', 'docx', 'txt', 'doc', 'ppt', 'pptx')"
        if category == "image":
            return "f.business_format IN ('jpeg', 'png')"
        if category == "ignored":
            return "f.support_status <> 'supported'"
        if category in {"unprocessed", "failed"}:
            return "f.processing_status = ?"
        raise ValueError("category must be all, table_file, document, image, ignored, unprocessed, or failed")

    def list_files(
        self,
        *,
        category: str | None = None,
        quality_status: str | None = None,
        source_format: str | None = None,
        query: str | None = None,
        limit: int = MAX_CATALOG_LIMIT,
        offset: int = 0,
        include_ignored: bool = False,
    ) -> dict[str, Any]:
        if limit < 1 or limit > MAX_CATALOG_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_CATALOG_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if quality_status not in {None, "ready", "needs_review", "unusable", "not_assessed"}:
            raise ValueError("quality must be ready, needs_review, unusable, or not_assessed")
        category_clause = self._file_category_clause(category)
        clauses = ["f.current_presence_state='present'"]
        params: list[Any] = []
        if category == "ignored":
            clauses.append("f.support_status <> 'supported'")
        elif not include_ignored:
            clauses.append("f.support_status='supported'")
        if category_clause:
            if category != "ignored":
                clauses.append(category_clause)
            if category in {"unprocessed", "failed"}:
                params.append("failed" if category == "failed" else "not_processed")
        if source_format:
            clauses.append("f.business_format=?")
            params.append(source_format.casefold().lstrip("."))
        if quality_status:
            clauses.append("f.quality_status=?")
            params.append(quality_status)
        if query and query.strip():
            clauses.append("(f.filename ILIKE ? OR f.relative_path ILIKE ?)")
            needle = f"%{query.strip()}%"
            params.extend((needle, needle))
        statement = f"""
            WITH current_assets AS (
                SELECT t.table_id AS asset_id,
                       'table' AS asset_type,
                       t.file_id,
                       t.content_sha256,
                       t.page_number,
                       t.source_kind,
                       t.extractor,
                       CAST(NULL AS BIGINT) AS chars,
                       f.business_format,
                       CASE
                           WHEN f.business_format = 'pdf'
                                OR t.source_kind IN ('page', 'image')
                                OR LOWER(t.extractor) LIKE '%img2table%' THEN 'needs_review'
                           WHEN t.quality_status = 'pass' THEN 'ready'
                           WHEN t.quality_status = 'fail' THEN 'unusable'
                           ELSE 'needs_review'
                       END AS base_quality
                FROM table_assets t
                JOIN files f ON f.file_id=t.file_id
                WHERE t.is_current=TRUE AND f.current_presence_state='present'
                UNION ALL
                SELECT t.text_asset_id AS asset_id,
                       'text' AS asset_type,
                       t.file_id,
                       t.content_sha256,
                       t.page_number,
                       t.source_kind,
                       t.extractor,
                       LENGTH(t.text) AS chars,
                       f.business_format,
                       CASE
                           WHEN LENGTH(TRIM(t.text)) = 0 THEN 'unusable'
                           WHEN LOWER(t.extractor) LIKE '%ocr%' THEN 'needs_review'
                           ELSE 'ready'
                       END AS base_quality
                FROM text_assets t
                JOIN files f ON f.file_id=t.file_id
                WHERE t.is_current=TRUE AND f.current_presence_state='present'
            ), latest_cleaning AS (
                SELECT * EXCLUDE (row_number)
                FROM (
                    SELECT c.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY c.asset_id, c.content_sha256
                               ORDER BY c.finished_at DESC NULLS LAST, c.started_at DESC, c.cleaning_run_id DESC
                           ) AS row_number
                    FROM cleaning_runs c
                    JOIN current_assets a ON a.asset_id=c.asset_id AND a.content_sha256=c.content_sha256
                ) ranked
                WHERE row_number=1
            ), issue_counts AS (
                SELECT q.asset_id,
                       COUNT(*) FILTER (WHERE q.status IN ('open','accepted')) AS issue_count
                FROM (
                    SELECT q.*, ROW_NUMBER() OVER (
                        PARTITION BY q.asset_id, q.issue_type
                        ORDER BY q.created_at DESC NULLS LAST, q.issue_id
                    ) AS issue_rank
                    FROM quality_issues q
                    WHERE q.issue_type <> 'possible_table_candidate'
                ) q
                JOIN current_assets a ON a.asset_id=q.asset_id
                LEFT JOIN latest_cleaning c ON c.cleaning_run_id=q.cleaning_run_id
                WHERE q.issue_rank=1
                  AND (q.cleaning_run_id IS NULL OR c.cleaning_run_id IS NOT NULL)
                GROUP BY q.asset_id
            ), semantic_current AS (
                SELECT asset_id, asset_type
                FROM (
                    SELECT s.asset_id,
                           s.asset_type,
                           ROW_NUMBER() OVER (
                               PARTITION BY s.asset_id, s.asset_type
                               ORDER BY s.current DESC, s.generated_at DESC, s.semantic_run_id DESC NULLS LAST
                           ) AS row_number
                    FROM semantic_metadata s
                    JOIN current_assets a ON a.asset_id=s.asset_id AND a.asset_type=s.asset_type
                    WHERE s.current=TRUE
                ) ranked
                WHERE row_number=1
            ), asset_values AS (
                SELECT a.file_id,
                       a.asset_type,
                       a.page_number,
                       COALESCE(c.quality_status, a.base_quality) AS quality_status,
                       CASE
                           WHEN a.asset_type='text' AND COALESCE(a.chars, 0) > 0 THEN 1
                           WHEN a.asset_type='table'
                                AND COALESCE(c.quality_status, a.base_quality)='ready'
                                AND COALESCE(a.source_kind, '') NOT IN ('page', 'image')
                                AND LOWER(COALESCE(a.extractor, '')) NOT LIKE '%img2table%'
                           THEN 1
                           ELSE 0
                       END AS usable_evidence,
                       COALESCE(i.issue_count, 0) AS quality_issue_count,
                       CASE WHEN s.asset_id IS NULL THEN 'pending' ELSE 'enriched' END AS semantic_status
                FROM current_assets a
                LEFT JOIN latest_cleaning c ON c.asset_id=a.asset_id AND c.content_sha256=a.content_sha256
                LEFT JOIN issue_counts i ON i.asset_id=a.asset_id
                LEFT JOIN semantic_current s ON s.asset_id=a.asset_id AND s.asset_type=a.asset_type
            ), asset_counts AS (
                SELECT file_id,
                       COUNT(*) FILTER (WHERE asset_type='table') AS table_assets,
                       COUNT(*) FILTER (WHERE asset_type='text') AS text_assets,
                       COUNT(*) FILTER (WHERE asset_type='text' AND page_number IS NOT NULL) AS text_pages_with_page,
                       COUNT(*) FILTER (WHERE asset_type='text' AND page_number IS NULL) AS text_assets_without_page,
                       COUNT(*) FILTER (WHERE quality_status='unusable') AS unusable_assets,
                       COUNT(*) FILTER (WHERE quality_status='needs_review') AS review_assets,
                       COUNT(*) FILTER (WHERE quality_status='ready') AS ready_assets,
                       SUM(usable_evidence) AS usable_evidence,
                       SUM(quality_issue_count) AS quality_issue_count,
                       COUNT(*) FILTER (WHERE semantic_status='enriched') AS semantic_enriched
                FROM asset_values GROUP BY file_id
            ), latest_routes AS (
                SELECT file_id, status,
                       ROW_NUMBER() OVER (PARTITION BY file_id, attempted_route ORDER BY finished_at DESC NULLS LAST, started_at DESC, extraction_run_id DESC) AS rn
                FROM extraction_runs
            ), route_counts AS (
                SELECT file_id,
                       COUNT(*) FILTER (WHERE rn=1 AND status IN ('successful','partial')) AS successful_routes,
                       COUNT(*) FILTER (WHERE rn=1 AND status='failed') AS failed_routes,
                       COUNT(*) FILTER (WHERE rn=1) AS route_count
                FROM latest_routes GROUP BY file_id
            ), aggregate AS (
                SELECT f.*, a.table_assets, a.text_assets,
                       CASE WHEN COALESCE(a.text_pages_with_page, 0) > 0 THEN a.text_pages_with_page
                            WHEN COALESCE(a.text_assets_without_page, 0) > 0 THEN 1 ELSE 0 END AS text_pages,
                       CASE WHEN f.support_status <> 'supported' THEN 'unsupported'
                            WHEN COALESCE(r.successful_routes, 0) > 0 AND COALESCE(a.usable_evidence, 0) = 0 THEN 'no_evidence'
                            WHEN COALESCE(r.successful_routes, 0) > 0 THEN 'ready'
                            WHEN COALESCE(r.failed_routes, 0) > 0 THEN 'failed'
                            WHEN COALESCE(r.route_count, 0) = 0 THEN 'not_processed'
                            ELSE 'processing' END AS processing_status,
                       CASE WHEN COALESCE(a.unusable_assets, 0) > 0 THEN 'unusable'
                            WHEN COALESCE(a.review_assets, 0) > 0 THEN 'needs_review'
                            WHEN COALESCE(a.ready_assets, 0) > 0 THEN 'ready'
                            ELSE 'not_assessed' END AS quality_status,
                       COALESCE(a.quality_issue_count, 0) AS quality_issue_count,
                       CASE WHEN f.support_status='supported' AND COALESCE(r.successful_routes, 0) > 0 AND COALESCE(a.usable_evidence, 0) = 0 THEN 'no_evidence'
                            WHEN f.support_status='supported' AND COALESCE(r.successful_routes, 0) > 0 THEN 'available'
                            ELSE 'not_assessed' END AS evidence_status,
                       CASE WHEN COALESCE(a.semantic_enriched, 0) > 0 THEN 'enriched' ELSE 'pending' END AS semantic_status
                FROM files f
                LEFT JOIN asset_counts a ON a.file_id=f.file_id
                LEFT JOIN route_counts r ON r.file_id=f.file_id
            )
            SELECT f.*, COUNT(*) OVER() AS _file_total
            FROM aggregate f WHERE {' AND '.join(clauses)}
            ORDER BY f.relative_path LIMIT ? OFFSET ?
        """
        registry = self._open()
        opened = time.perf_counter_ns()
        try:
            cursor = registry.connection.execute(statement, [*params, limit, offset])
            columns = [item[0] for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            if rows:
                total = int(rows[0].pop("_file_total", 0) or 0)
                for row in rows[1:]:
                    row.pop("_file_total", None)
            else:
                # The window total has no row to travel back on an empty page.
                # Keep the empty-page fallback bounded to the same aggregate
                # statement; normal catalog pages now need only one query.
                count_cursor = registry.connection.execute(
                    f"SELECT COUNT(*) FROM ({statement.replace('SELECT f.*, COUNT(*) OVER() AS _file_total', 'SELECT f.file_id').replace('ORDER BY f.relative_path LIMIT ? OFFSET ?', '')}) files_count",
                    params,
                )
                total = int(count_cursor.fetchone()[0] or 0)
            insight_statuses = self._file_insight_statuses(rows)
            for row in rows:
                row["file_insight_status"] = insight_statuses.get(str(row.get("file_id") or ""), "not_started")
            items = [self._file_item(row) for row in rows]
            return {
                "items": items,
                "pagination": {"limit": limit, "offset": offset, "total": total, "hasNext": offset + len(rows) < total},
                "timings": {"query_ms": round((time.perf_counter_ns() - opened) / 1_000_000, 3)},
            }
        finally:
            registry.close()

    @staticmethod
    def _cursor_rows(cursor: Any) -> list[dict[str, Any]]:
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def _direct_file_row(registry: Registry, file_id: str) -> dict[str, Any] | None:
        """Read one present file directly; never build the catalog page first."""

        cursor = registry.connection.execute(
            "SELECT * FROM files WHERE file_id=? AND current_presence_state='present' LIMIT 1",
            [file_id],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(zip([item[0] for item in cursor.description], row))

    @staticmethod
    def _file_asset_counts(registry: Registry, file_id: str) -> dict[str, Any]:
        cursor = registry.connection.execute(
            f"""
            WITH assets AS ({FILE_ASSET_ROWS_QUERY})
            SELECT
                COUNT(*) FILTER (WHERE asset_type='table') AS table_assets,
                COUNT(*) FILTER (WHERE asset_type='text') AS text_assets,
                COUNT(*) FILTER (WHERE asset_type='text' AND page_number IS NOT NULL) AS text_pages_with_page,
                COUNT(*) FILTER (WHERE asset_type='text' AND page_number IS NULL) AS text_assets_without_page,
                COUNT(*) FILTER (WHERE quality_status='unusable') AS unusable_assets,
                COUNT(*) FILTER (WHERE quality_status='needs_review') AS review_assets,
                COUNT(*) FILTER (WHERE quality_status='ready') AS ready_assets,
                COALESCE(SUM(CASE
                    WHEN asset_type='text' AND COALESCE(chars, 0) > 0 THEN 1
                    WHEN asset_type='table'
                         AND quality_status='ready'
                         AND COALESCE(source_kind, '') NOT IN ('page', 'image')
                         AND LOWER(COALESCE(extractor, '')) NOT LIKE '%img2table%'
                    THEN 1
                    ELSE 0
                END), 0) AS usable_evidence,
                COALESCE(SUM(quality_issue_count), 0) AS quality_issue_count,
                COUNT(*) FILTER (WHERE semantic_status='enriched') AS semantic_enriched
            FROM assets
            """,
            [file_id],
        )
        row = cursor.fetchone()
        if row is None:
            return {}
        return dict(zip([item[0] for item in cursor.description], row))

    @staticmethod
    def _file_route_counts(registry: Registry, file_id: str) -> dict[str, int]:
        cursor = registry.connection.execute(
            """
            WITH latest AS (
                SELECT status,
                       ROW_NUMBER() OVER (
                           PARTITION BY attempted_route
                           ORDER BY finished_at DESC NULLS LAST,
                                    started_at DESC,
                                    extraction_run_id DESC
                       ) AS rn
                FROM extraction_runs
                WHERE file_id=?
            )
            SELECT
                COUNT(*) FILTER (WHERE rn=1 AND status IN ('successful','partial')) AS successful_routes,
                COUNT(*) FILTER (WHERE rn=1 AND status='failed') AS failed_routes,
                COUNT(*) FILTER (WHERE rn=1) AS route_count
            FROM latest
            """,
            [file_id],
        )
        row = cursor.fetchone()
        if row is None:
            return {"successful_routes": 0, "failed_routes": 0, "route_count": 0}
        return {
            str(key): int(value or 0)
            for key, value in zip([item[0] for item in cursor.description], row)
        }

    @classmethod
    def _file_item_from_snapshot(
        cls,
        file_row: Mapping[str, Any],
        asset_counts: Mapping[str, Any],
        route_counts: Mapping[str, int],
    ) -> dict[str, Any]:
        combined = dict(file_row)
        combined.update(asset_counts)
        combined.update(route_counts)
        table_assets = int(asset_counts.get("table_assets") or 0)
        text_assets = int(asset_counts.get("text_assets") or 0)
        text_pages = int(asset_counts.get("text_pages_with_page") or 0)
        if not text_pages and text_assets:
            text_pages = 1
        if str(file_row.get("support_status") or "") != "supported":
            processing_status = "unsupported"
        elif int(route_counts.get("successful_routes") or 0) > 0 and int(asset_counts.get("usable_evidence") or 0) == 0:
            processing_status = "no_evidence"
        elif int(route_counts.get("successful_routes") or 0) > 0:
            processing_status = "ready"
        elif int(route_counts.get("failed_routes") or 0) > 0:
            processing_status = "failed"
        elif int(route_counts.get("route_count") or 0) == 0:
            processing_status = "not_processed"
        else:
            processing_status = "processing"
        if int(asset_counts.get("unusable_assets") or 0) > 0:
            quality_status = "unusable"
        elif int(asset_counts.get("review_assets") or 0) > 0:
            quality_status = "needs_review"
        elif int(asset_counts.get("ready_assets") or 0) > 0:
            quality_status = "ready"
        else:
            quality_status = "not_assessed"
        combined.update(
            {
                "table_assets": table_assets,
                "text_assets": text_assets,
                "text_pages": text_pages,
                "processing_status": processing_status,
                "evidence_status": (
                    "no_evidence"
                    if int(route_counts.get("successful_routes") or 0) > 0 and int(asset_counts.get("usable_evidence") or 0) == 0
                    else "available"
                    if int(route_counts.get("successful_routes") or 0) > 0
                    else "not_assessed"
                ),
                "quality_status": quality_status,
                "quality_issue_count": int(asset_counts.get("quality_issue_count") or 0),
                "semantic_status": "enriched" if int(asset_counts.get("semantic_enriched") or 0) > 0 else "pending",
            }
        )
        return cls._file_item(combined)

    def _file_insight_statuses(self, rows: list[Mapping[str, Any]]) -> dict[str, str]:
        """Add the file-level insight state without reopening the registry."""

        try:
            from .file_insight import FileInsightQueueStore, FileInsightStore

            queue = {str(item.get("file_id") or ""): item for item in FileInsightQueueStore(self.workspace_root).entries()}
            store = FileInsightStore(self.workspace_root)
        except (ImportError, OSError):
            return {}
        valid_phases = {"requesting_model", "validating", "persisting"}
        statuses: dict[str, str] = {}
        for row in rows:
            file_id = str(row.get("file_id") or "")
            if not file_id:
                continue
            if str(row.get("processing_status") or "") == "no_evidence" or str(row.get("evidence_status") or "") == "no_evidence":
                statuses[file_id] = "no_evidence"
                continue
            entry = queue.get(file_id)
            entry_status = str(entry.get("status") or "") if entry else ""
            if entry_status == "queued":
                statuses[file_id] = "queued"
                continue
            if entry_status == "running":
                phase = str(entry.get("phase") or "requesting_model")
                statuses[file_id] = phase if phase in valid_phases else "requesting_model"
                continue
            if entry_status == "failed":
                statuses[file_id] = "failed"
                continue
            record = store.read_current(file_id, str(row.get("sha256") or ""))
            record_status = str(record.get("status") or "") if record else ""
            statuses[file_id] = "completed" if record_status == "completed" else ("failed" if record_status == "failed" else "not_started")
        return statuses

    def _file_snapshot(
        self,
        registry: Registry,
        file_id: str,
        *,
        asset_limit: int | None = MAX_FILE_CHILD_ASSETS,
    ) -> dict[str, Any] | None:
        """Load a file-keyed snapshot; only the primary content view is bounded."""

        file_row = self._direct_file_row(registry, file_id)
        if file_row is None:
            return None
        statement = f"""
            SELECT * FROM ({FILE_ASSET_ROWS_QUERY}) assets
            ORDER BY page_number NULLS LAST, sheet_name NULLS LAST, asset_type, asset_id
        """
        params: list[Any] = [file_id]
        if asset_limit is not None:
            statement += " LIMIT ?"
            params.append(asset_limit)
        asset_cursor = registry.connection.execute(statement, params)
        asset_rows = self._cursor_rows(asset_cursor)
        issue_counts = registry.visible_quality_issue_counts(asset_rows)
        for row in asset_rows:
            row["quality_issue_count"] = issue_counts.get(str(row.get("asset_id") or ""), 0)
        asset_counts = self._file_asset_counts(registry, file_id)
        # The bounded child rows' issue counts are deliberately corrected
        # above. Keep the file-level count consistent with them when the
        # bounded child list contains the complete file.
        if int(asset_counts.get("table_assets") or 0) + int(asset_counts.get("text_assets") or 0) <= len(asset_rows):
            asset_counts = dict(asset_counts)
            asset_counts["quality_issue_count"] = sum(issue_counts.values())
        return {
            "file_row": file_row,
            "file": self._file_item_from_snapshot(
                file_row,
                asset_counts,
                self._file_route_counts(registry, file_id),
            ),
            "asset_rows": asset_rows,
        }

    def _text_for_row(self, row: Mapping[str, Any], *, limit: int) -> tuple[str, bool]:
        value = row.get("normalized_artifact_path") or row.get("raw_artifact_path")
        if value:
            path = artifact_absolute(str(value), self.workspace_root)
            if path.is_file():
                return self._read_text_window(path, 0, limit)
        # Missing artifacts are exceptional, but a bounded DB fallback keeps
        # the file view useful while the advanced asset endpoint can still
        # report the artifact problem explicitly.
        registry = self._open()
        try:
            cursor = registry.connection.execute(
                "SELECT text FROM text_assets WHERE text_asset_id=? AND is_current=TRUE LIMIT 1",
                [row.get("asset_id")],
            )
            found = cursor.fetchone()
        finally:
            registry.close()
        raw = str(found[0] or "") if found else ""
        return raw[:limit], len(raw) > limit

    def _metadata_for_row(self, row: Mapping[str, Any]) -> Mapping[str, Any]:
        value = row.get("metadata_artifact_path")
        if not value:
            return {}
        try:
            metadata = _parse_json_file(artifact_absolute(str(value), self.workspace_root))
        except (OSError, ValueError):
            return {}
        return metadata if isinstance(metadata, Mapping) else {}

    def _bounded_table_preview(self, row: Mapping[str, Any], *, layer: str = "normalized") -> dict[str, Any] | None:
        if layer not in {"raw", "normalized"}:
            raise ValueError("table preview layer must be raw or normalized")
        value = row.get("raw_artifact_path") if layer == "raw" else row.get("normalized_artifact_path")
        if not value:
            return None
        try:
            path = artifact_absolute(str(value), self.workspace_root)
            if not path.is_file():
                return None
            scan = pl.scan_parquet(str(path))
            schema_columns = list(scan.collect_schema().names())
            selected_columns = schema_columns[:MAX_FILE_CONTENT_TABLE_COLUMNS]
            frame = scan.select(selected_columns).slice(0, MAX_FILE_CONTENT_TABLE_ROWS).collect()
            total_value = row.get("rows")
            total = int(total_value) if total_value is not None else None
            cell_values_truncated = False
            records: list[dict[str, Any]] = []
            for record in frame.to_dicts():
                bounded_record: dict[str, Any] = {}
                for column, value in record.items():
                    if isinstance(value, str) and len(value) > MAX_FILE_CONTENT_CELL_CHARS:
                        bounded_record[column] = value[: MAX_FILE_CONTENT_CELL_CHARS - 1] + "…"
                        cell_values_truncated = True
                    else:
                        bounded_record[column] = value
                records.append(bounded_record)
            metadata = self._metadata_for_row(row)
            header_detected = False
            if layer == "raw":
                header_detected = bool(metadata.get("header_detected"))
                source_range = metadata.get("source_range")
                row_mapping = metadata.get("normalized_row_mapping")
                if isinstance(source_range, Mapping) and isinstance(row_mapping, Mapping):
                    try:
                        header_detected = header_detected or int(row_mapping.get("parquet_row_zero_maps_to_source_row")) > int(source_range.get("row_start", 0))
                    except (TypeError, ValueError):
                        pass
            presentation_columns: list[str] | None = None
            if layer == "raw":
                mappings = metadata.get("column_mapping")
                mapped_headers: list[str | None] = []
                if isinstance(mappings, list):
                    for index, column in enumerate(frame.columns):
                        mapping = mappings[index] if index < len(mappings) and isinstance(mappings[index], Mapping) else {}
                        original = mapping.get("original")
                        mapped_headers.append(str(original).strip() if original is not None and str(original).strip() else None)
                metadata_headers = metadata.get("headers")
                if isinstance(metadata_headers, list) and len(metadata_headers) >= len(frame.columns):
                    mapped_headers = [
                        str(metadata_headers[index]).strip() if metadata_headers[index] is not None and str(metadata_headers[index]).strip() else None
                        for index in range(len(frame.columns))
                    ]
                if any(mapped_headers):
                    header_detected = True
                    presentation_columns = [mapped or column for mapped, column in zip(mapped_headers, frame.columns)]
            if presentation_columns is None and header_detected and records:
                # Legacy artifacts may not contain column_mapping. Preserve
                # their existing presentation behavior, but never replace a
                # known original header with a data-row value.
                first_record = records[0]
                presentation_columns = [
                    str(first_record.get(column) or "").strip() or column
                    for column in frame.columns
                ]
            return _json_value(
                {
                    "assetId": row.get("asset_id"),
                    "assetType": "table",
                    "layer": layer,
                    "columns": frame.columns,
                    "rows": records,
                    "headerDetected": header_detected,
                    "presentationColumns": presentation_columns,
                    "presentation": metadata.get("presentation") if isinstance(metadata.get("presentation"), Mapping) else None,
                    "columnsTruncated": len(schema_columns) > MAX_FILE_CONTENT_TABLE_COLUMNS,
                    "cellValuesTruncated": cell_values_truncated,
                    "pagination": {
                        "limit": MAX_FILE_CONTENT_TABLE_ROWS,
                        "offset": 0,
                        "total": total,
                        "hasNext": (total is not None and total > MAX_FILE_CONTENT_TABLE_ROWS)
                        or (total is None and len(frame) == MAX_FILE_CONTENT_TABLE_ROWS),
                    },
                }
            )
        except (OSError, ValueError, pl.exceptions.PolarsError):
            return None

    @staticmethod
    def _content_provenance(row: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
        block_bboxes = []
        metadata_items = metadata.get("blocks", metadata.get("ocr_blocks", [])) if isinstance(metadata, Mapping) else []
        for item in metadata_items:
            if isinstance(item, Mapping) and _content_bbox(item.get("bbox")) is not None:
                block_bboxes.append(item.get("bbox"))
        return _json_value(
            {
                "fileId": row.get("file_id"),
                "assetId": row.get("asset_id"),
                "relativePath": row.get("source_file"),
                "sha256": row.get("content_sha256"),
                "extractor": row.get("extractor"),
                "extractorVersion": row.get("extractor_version"),
                "sourceKind": row.get("source_kind"),
                "pageNumber": row.get("page_number"),
                "sheetName": row.get("sheet_name"),
                "sheetIndex": metadata.get("sheet_index"),
                "documentOrder": metadata.get("document_order"),
                "tableIndex": metadata.get("table_index"),
                "extractionRunId": row.get("extraction_run_id"),
                "sourceRange": metadata.get("source_range"),
                "bbox": metadata.get("candidate_bbox", metadata.get("bbox")),
                "blockBboxes": block_bboxes or None,
            }
        )

    def _content_table_block(self, row: Mapping[str, Any]) -> dict[str, Any]:
        metadata = self._metadata_for_row(row)
        candidate = str(metadata.get("candidate_status") or "").casefold() == "candidate" or str(row.get("source_kind") or "").casefold() == "image" or "img2table" in str(row.get("extractor") or "").casefold()
        raw_preview = self._bounded_table_preview(row, layer="raw")
        normalized_preview = self._bounded_table_preview(row, layer="normalized")
        preview = raw_preview or normalized_preview
        return {
            "type": "table",
            "assetId": row.get("asset_id"),
            "displayName": row.get("effective_display_name") or row.get("fallback_display_name"),
            "pageNumber": row.get("page_number"),
            "sheetName": row.get("sheet_name"),
            "candidate": candidate,
            "candidateStatus": metadata.get("candidate_status") if candidate else None,
            "parserValidity": metadata.get("parser_validity"),
            "qualityStatus": row.get("quality_status"),
            "preview": preview,
            "rawPreview": raw_preview,
            "normalizedPreview": normalized_preview,
            "presentation": metadata.get("presentation") if isinstance(metadata.get("presentation"), Mapping) else None,
            "previewLayer": preview.get("layer") if isinstance(preview, Mapping) else None,
            "previewAvailable": preview is not None,
            "provenance": self._content_provenance(row, metadata),
        }

    def _content_text_elements(self, row: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return bounded display fragments from existing PDF/OCR metadata."""

        metadata = self._metadata_for_row(row)
        raw_elements = metadata.get("blocks") or metadata.get("ocr_blocks")
        if not isinstance(raw_elements, list):
            return []
        elements: list[dict[str, Any]] = []
        for index, item in enumerate(raw_elements):
            if not isinstance(item, Mapping):
                continue
            text = item.get("normalized_text") or item.get("text") or item.get("raw_text")
            if not isinstance(text, str) or not text.strip():
                continue
            elements.append(
                {
                    "text": _presentation_text(text),
                    "bbox": _content_bbox(item.get("bbox")),
                    "index": index,
                }
            )
        return elements

    def _content_table_bbox(self, row: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
        metadata = self._metadata_for_row(row)
        return _content_bbox(metadata.get("candidate_bbox") or metadata.get("bbox"))

    def _spatial_content_items(
        self,
        text_rows: list[Mapping[str, Any]],
        table_rows: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Compose text/table fragments only from reliable existing geometry."""

        table_boxes = [self._content_table_bbox(row) for row in table_rows]
        items: list[dict[str, Any]] = []
        sequence = 0
        for row in text_rows:
            fragments = self._content_text_elements(row)
            if fragments:
                for fragment in fragments:
                    bbox = fragment.get("bbox")
                    # When both sides expose the same coordinate system, do
                    # not repeat native/OCR text that lies inside a detected
                    # table candidate.  Missing geometry never triggers this
                    # filter; it is safer to show duplicate text than to hide
                    # source content without evidence.
                    if bbox and any(box and _bbox_overlap_ratio(bbox, box) >= 0.55 for box in table_boxes):
                        continue
                    items.append({"kind": "text", "row": row, "text": fragment["text"], "bbox": bbox, "sequence": sequence})
                    sequence += 1
            else:
                text, _truncated = self._text_for_row(row, limit=MAX_FILE_TEXT_PREVIEW_CHARS)
                if text.strip():
                    items.append({"kind": "text", "row": row, "text": _presentation_text(text), "bbox": None, "sequence": sequence})
                    sequence += 1
        for row in table_rows:
            bbox = self._content_table_bbox(row)
            items.append({"kind": "table", "row": row, "text": None, "bbox": bbox, "sequence": sequence})
            sequence += 1
        return sorted(
            items,
            key=lambda item: (
                item["bbox"] is None,
                round(item["bbox"][1], 3) if item["bbox"] else 0,
                round(item["bbox"][0], 3) if item["bbox"] else 0,
                item["sequence"],
            ),
        )

    def _content_text_block(
        self,
        row: Mapping[str, Any],
        *,
        text: str | None = None,
        limit: int,
        order: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        value = text
        truncated = False
        if value is None:
            value, truncated = self._text_for_row(row, limit=limit)
        else:
            truncated = len(value) > limit
            value = value[:limit]
        presented = _presentation_text(value)
        truncated = truncated or len(presented) > limit
        value = presented[:limit]
        metadata = self._metadata_for_row(row)
        return (
            {
                "type": "text",
                "assetId": row.get("asset_id"),
                "text": value,
                "truncated": truncated,
                "textOffset": 0,
                "nextTextOffset": len(value),
                "continuationAvailable": truncated,
                "order": order,
                "pageNumber": row.get("page_number"),
                "sheetName": row.get("sheet_name"),
                "provenance": self._content_provenance(row, metadata),
            },
            len(value),
        )

    def _source_path(self, file_row: Mapping[str, Any]) -> Path:
        """Resolve a registered source without allowing path escape."""

        source_root_value = str(file_row.get("source_root") or "").strip()
        relative_value = str(file_row.get("relative_path") or "").strip()
        if not source_root_value or not relative_value:
            raise FileNotFoundError("source file path is unavailable")
        source_root = Path(source_root_value).expanduser().resolve()
        source_path = (source_root / Path(relative_value)).resolve()
        try:
            source_path.relative_to(source_root)
        except ValueError as exc:
            raise FileNotFoundError("source file path is outside the registered source root") from exc
        if not source_path.is_file():
            raise FileNotFoundError("source file is unavailable")
        return source_path

    def source_file(self, file_id: str) -> tuple[Path, str, str]:
        """Resolve one registered source for the read-only native viewer."""

        registry = self._open()
        try:
            file_row = self._direct_file_row(registry, file_id)
        finally:
            registry.close()
        if file_row is None:
            raise FileNotFoundError("file was not found")
        source_path = self._source_path(file_row)
        fmt = str(file_row.get("business_format") or "").casefold()
        content_types = {
            "pdf": "application/pdf",
            "csv": "text/csv; charset=utf-8",
            "tsv": "text/tab-separated-values; charset=utf-8",
            "txt": "text/plain; charset=utf-8",
            "png": "image/png",
            "jpeg": "image/jpeg",
            "jpg": "image/jpeg",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        content_type = content_types.get(fmt) or mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        return source_path, content_type, source_path.name

    def _preview_cache_path(self, file_id: str, page_number: int) -> Path:
        safe_id = "".join(character for character in str(file_id) if character.isalnum() or character in {"-", "_"})
        if not safe_id:
            raise ValueError("file id is invalid")
        return self.workspace_root.parent / "cache" / "previews" / safe_id / f"page-{page_number}.png"

    @staticmethod
    def _write_preview_cache(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex[:12]}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def file_preview(
        self,
        file_id: str,
        *,
        page: int = 1,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> tuple[bytes, str]:
        """Render one bounded source preview for the file reading surface.

        PDF pages are rendered on demand and cached below the resettable
        project cache. Image sources are served as bounded immutable bytes;
        neither path writes beside the user's source file.
        """

        if page < 1:
            raise ValueError("page must be positive")
        registry = self._open()
        try:
            file_row = self._direct_file_row(registry, file_id)
        finally:
            registry.close()
        if file_row is None:
            raise FileNotFoundError("file was not found")
        source_path = self._source_path(file_row)
        fmt = str(file_row.get("business_format") or "").casefold()
        if fmt in {"png", "jpeg", "jpg"}:
            if source_path.stat().st_size > MAX_SOURCE_PREVIEW_BYTES:
                raise ValueError("source image exceeds the local preview limit")
            if bbox is not None:
                try:
                    import pymupdf
                except ImportError as exc:  # pragma: no cover - provisioning/doctor owns this boundary
                    raise RuntimeError("PyMuPDF is not provisioned for source preview") from exc
                with pymupdf.open(str(source_path)) as document:
                    page_image = document.load_page(0)
                    clip = pymupdf.Rect(*bbox) & page_image.rect
                    if clip.is_empty or clip.width < 1 or clip.height < 1:
                        raise ValueError("preview crop is outside the source image")
                    largest_dimension = max(float(clip.width), float(clip.height), 1.0)
                    scale = min(2.0, MAX_SOURCE_PREVIEW_DIMENSION / largest_dimension)
                    content = page_image.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False).tobytes("png")
                if len(content) > MAX_SOURCE_PREVIEW_BYTES:
                    raise ValueError("source image crop exceeds the local preview limit")
                return content, "image/png"
            content_type = "image/png" if fmt == "png" else "image/jpeg"
            return source_path.read_bytes(), content_type
        if fmt != "pdf":
            raise ValueError("source preview is not available for this file format")

        # Crops are intentionally not cached under the page cache key: a
        # table candidate may have many rectangles on one page.
        cache_path = self._preview_cache_path(file_id, page) if bbox is None else None
        if cache_path is not None and cache_path.is_file() and cache_path.stat().st_size <= MAX_SOURCE_PREVIEW_BYTES:
            return cache_path.read_bytes(), "image/png"
        try:
            import pymupdf
        except ImportError as exc:  # pragma: no cover - provisioning/doctor owns this boundary
            raise RuntimeError("PyMuPDF is not provisioned for source preview") from exc
        with pymupdf.open(str(source_path)) as document:
            if page > document.page_count:
                raise ValueError("requested PDF page is outside the document")
            source_page = document.load_page(page - 1)
            rect = source_page.rect
            clip = None
            if bbox is not None:
                clip = pymupdf.Rect(*bbox) & rect
                if clip.is_empty or clip.width < 1 or clip.height < 1:
                    raise ValueError("preview crop is outside the PDF page")
            largest_dimension = max(float(rect.width), float(rect.height), 1.0)
            scale = min(2.0, MAX_SOURCE_PREVIEW_DIMENSION / largest_dimension)
            pixmap = source_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
            content = pixmap.tobytes("png")
        if len(content) > MAX_SOURCE_PREVIEW_BYTES:
            raise ValueError("rendered PDF page exceeds the local preview limit")
        try:
            if cache_path is not None:
                self._write_preview_cache(cache_path, content)
        except OSError:
            # Rendering remains useful if a cache directory is temporarily
            # unavailable; reset can remove any successfully written cache.
            pass
        return content, "image/png"

    @staticmethod
    def _source_preview_descriptor(file_row: Mapping[str, Any], file_id: str) -> dict[str, Any]:
        fmt = str(file_row.get("business_format") or "").casefold()
        available = fmt in {"pdf", "png", "jpeg", "jpg"}
        return {
            "available": available,
            "kind": "pdf_page" if fmt == "pdf" else ("image" if fmt in {"png", "jpeg", "jpg"} else None),
            "url": f"/api/v1/files/{quote(str(file_id), safe='')}/preview" if available else None,
            "sourceUrl": f"/api/v1/files/{quote(str(file_id), safe='')}/source",
            "pageParameter": "page" if fmt == "pdf" else None,
        }

    def file_detail(self, file_id: str) -> dict[str, Any] | None:
        started = time.perf_counter_ns()
        registry = self._open()
        try:
            queried = time.perf_counter_ns()
            snapshot = self._file_snapshot(registry, file_id, asset_limit=None)
            query_ms = (time.perf_counter_ns() - queried) / 1_000_000
            if snapshot is None:
                return None
            file_row = snapshot["file_row"]
            asset_rows = snapshot["asset_rows"]
            text_rows = sorted(
                (row for row in asset_rows if row.get("asset_type") == "text"),
                key=lambda row: (row.get("page_number") is None, row.get("page_number") or 0, str(row.get("asset_id") or "")),
            )
            pages: list[dict[str, Any]] = []
            consumed = 0
            for row in text_rows:
                remaining = max(0, MAX_FILE_TEXT_PREVIEW_CHARS - consumed)
                snippet, has_next = self._text_for_row(row, limit=remaining) if remaining else ("", True)
                consumed += len(snippet)
                pages.append(
                    {
                        "assetId": row.get("asset_id"),
                        "pageNumber": row.get("page_number"),
                        "section": row.get("sheet_name"),
                        "text": snippet,
                        "truncated": has_next,
                        "source": {
                            "fileId": file_id,
                            "relativePath": file_row.get("relative_path"),
                            "sha256": file_row.get("sha256"),
                            "pageNumber": row.get("page_number"),
                        },
                    }
                )
            detail = dict(snapshot["file"])
            insight_statuses = self._file_insight_statuses([file_row])
            detail["fileInsightStatus"] = insight_statuses.get(str(file_id), "not_started")
            detail["textAssets"] = len(text_rows)
            detail["textPages"] = sum(1 for row in text_rows if row.get("page_number") is not None) or (1 if text_rows else 0)
            detail["tableAssets"] = sum(1 for row in asset_rows if row.get("asset_type") == "table")
            detail["qualityIssueCount"] = sum(int(row.get("quality_issue_count") or 0) for row in asset_rows)
            result = {
                "file": detail,
                "pages": pages,
                "tables": [self._catalog_item(row) for row in asset_rows if row.get("asset_type") == "table"],
                "assets": [self._catalog_item(row) for row in asset_rows],
                "source": {
                    "fileId": file_id,
                    "relativePath": file_row.get("relative_path"),
                    "sha256": file_row.get("sha256"),
                    "root": file_row.get("source_root"),
                    "format": file_row.get("business_format"),
                },
                "timings": {
                    "query_ms": round(query_ms, 3),
                    "total_ms": round((time.perf_counter_ns() - started) / 1_000_000, 3),
                },
            }
            return _json_value(result)
        finally:
            registry.close()

    def file_content(
        self,
        file_id: str,
        *,
        page: int | None = None,
        sheet: str | None = None,
    ) -> dict[str, Any] | None:
        """Return bounded mixed content for the file-level primary view."""

        started = time.perf_counter_ns()
        cache_key = (str(file_id), page, sheet)
        with self._content_cache_lock:
            cached = copy.deepcopy(self._content_cache.get(cache_key))
        if cached is not None:
            result = cached
            result.setdefault("timings", {})["cache_hit"] = True
            result["timings"]["total_ms"] = round((time.perf_counter_ns() - started) / 1_000_000, 3)
            return result
        registry = self._open()
        try:
            queried = time.perf_counter_ns()
            snapshot = self._file_snapshot(registry, file_id, asset_limit=MAX_FILE_CHILD_ASSETS)
            query_ms = (time.perf_counter_ns() - queried) / 1_000_000
            if snapshot is None:
                return None
            file_row = snapshot["file_row"]
            asset_rows = snapshot["asset_rows"]
            text_rows = sorted(
                (row for row in asset_rows if row.get("asset_type") == "text"),
                key=lambda row: (row.get("page_number") is None, row.get("page_number") or 0, str(row.get("asset_id") or "")),
            )
            table_rows = [row for row in asset_rows if row.get("asset_type") == "table"]
            fmt = str(file_row.get("business_format") or "").casefold()
            navigation: dict[str, Any] = {"kind": "continuous", "current": None, "total": None}
            if fmt == "pdf":
                page_numbers = sorted({int(row["page_number"]) for row in [*text_rows, *table_rows] if row.get("page_number") is not None})
                total_pages = self._pdf_page_counts.get(str(file_id), max(page_numbers, default=0))
                if str(file_id) not in self._pdf_page_counts:
                    try:
                        import pymupdf

                        with pymupdf.open(str(self._source_path(file_row))) as document:
                            total_pages = document.page_count
                        self._pdf_page_counts[str(file_id)] = total_pages
                    except (OSError, RuntimeError, ValueError):
                        pass
                if page is not None:
                    selected_page = int(page)
                    if selected_page < 1 or (total_pages and selected_page > total_pages):
                        raise ValueError("requested PDF page is outside the document")
                    text_rows = [row for row in text_rows if row.get("page_number") == selected_page]
                    table_rows = [row for row in table_rows if row.get("page_number") == selected_page]
                    navigation = {"kind": "page", "current": selected_page, "total": total_pages}
                else:
                    # Keep direct CatalogService callers backward compatible;
                    # the viewer supplies page=1 and remains single-page.
                    navigation = {"kind": "page", "current": None, "total": total_pages}
            elif fmt in {"xls", "xlsx"}:
                sheet_names = list(dict.fromkeys(str(row.get("sheet_name") or "Sheet") for row in table_rows))
                selected_sheet = sheet
                if selected_sheet is not None:
                    table_rows = [row for row in table_rows if str(row.get("sheet_name") or "Sheet") == selected_sheet]
                navigation = {"kind": "sheet", "current": selected_sheet, "items": sheet_names, "total": len(sheet_names)}
            elif fmt == "docx":
                navigation = {"kind": "document", "current": "document", "total": 1}
            elif fmt == "txt":
                navigation = {"kind": "continuous", "current": "text", "total": 1}
            elif fmt in {"png", "jpeg"}:
                navigation = {"kind": "image", "current": "image", "total": 1}
            sections: list[dict[str, Any]] = []
            text_budget = MAX_FILE_TEXT_PREVIEW_CHARS
            emitted_blocks = 0
            emitted_tables = 0
            content_truncated = (
                int(snapshot["file"].get("textAssets") or 0) > len(text_rows)
                or int(snapshot["file"].get("tableAssets") or 0) > len(table_rows)
            )

            def add_text(blocks: list[dict[str, Any]], row: Mapping[str, Any], *, text: str | None = None, order: int | None = None) -> None:
                nonlocal content_truncated, emitted_blocks, text_budget
                if emitted_blocks >= MAX_FILE_CONTENT_BLOCKS:
                    content_truncated = True
                    return
                if text_budget <= 0:
                    content_truncated = True
                    return
                if text is not None and not text.strip():
                    return
                block, used = self._content_text_block(row, text=text, limit=text_budget, order=order)
                text_budget = max(0, text_budget - used)
                emitted_blocks += 1
                content_truncated = content_truncated or bool(block.get("truncated"))
                blocks.append(block)

            def add_table(blocks: list[dict[str, Any]], row: Mapping[str, Any], *, order: int | None = None) -> None:
                nonlocal content_truncated, emitted_blocks, emitted_tables
                if emitted_blocks >= MAX_FILE_CONTENT_BLOCKS or emitted_tables >= MAX_FILE_CONTENT_TABLES:
                    content_truncated = True
                    return
                block = self._content_table_block(row)
                block["order"] = order
                emitted_blocks += 1
                emitted_tables += 1
                preview = block.get("preview")
                if isinstance(preview, Mapping) and isinstance(preview.get("pagination"), Mapping):
                    content_truncated = content_truncated or bool(preview["pagination"].get("hasNext"))
                if isinstance(preview, Mapping):
                    content_truncated = content_truncated or bool(preview.get("columnsTruncated"))
                    content_truncated = content_truncated or bool(preview.get("cellValuesTruncated"))
                blocks.append(block)

            if fmt == "pdf":
                page_numbers = sorted({int(row["page_number"]) for row in [*text_rows, *table_rows] if row.get("page_number") is not None})
                for page_number in page_numbers:
                    blocks: list[dict[str, Any]] = []
                    page_text_rows = [row for row in text_rows if row.get("page_number") == page_number]
                    page_table_rows = [row for row in table_rows if row.get("page_number") == page_number]
                    pending_text: list[Mapping[str, Any]] = []

                    def flush_page_text() -> None:
                        if not pending_text:
                            return
                        first = pending_text[0]
                        combined = _join_spatial_text(pending_text)
                        pending_text.clear()
                        add_text(blocks, first["row"], text=combined)

                    for item in self._spatial_content_items(page_text_rows, page_table_rows):
                        if item["kind"] == "text":
                            pending_text.append(item)
                        else:
                            flush_page_text()
                            add_table(blocks, item["row"])
                    flush_page_text()
                    if blocks:
                        sections.append({"sectionId": f"page-{page_number}", "kind": "page", "label": f"Page {page_number}", "pageNumber": page_number, "sheetName": None, "blocks": blocks})
            elif fmt == "docx":
                blocks: list[dict[str, Any]] = []
                pending_text: list[tuple[Mapping[str, Any], str, int]] = []

                def flush_docx_text() -> None:
                    if not pending_text:
                        return
                    first_row, _first_text, first_order = pending_text[0]
                    combined = _presentation_text("\n\n".join(item[1] for item in pending_text))
                    pending_text.clear()
                    add_text(blocks, first_row, text=combined, order=first_order)

                document_items: list[dict[str, Any]] = []
                for row in text_rows:
                    metadata = self._metadata_for_row(row)
                    paragraphs = metadata.get("paragraphs")
                    if isinstance(paragraphs, list) and paragraphs:
                        for paragraph in paragraphs:
                            if not isinstance(paragraph, Mapping):
                                continue
                            paragraph_order = paragraph.get("document_order")
                            if paragraph_order is None:
                                paragraph_order = paragraph.get("paragraph_index", 0)
                            text = str(paragraph.get("text") or "")
                            if text.strip():
                                document_items.append({"kind": "text", "row": row, "text": text, "order": int(paragraph_order or 0)})
                    else:
                        document_items.append({"kind": "text", "row": row, "text": None, "order": 0})
                for index, row in enumerate(table_rows):
                    metadata = self._metadata_for_row(row)
                    table_order = metadata.get("document_order")
                    if table_order is None:
                        table_order = (index + 1) * 2
                    document_items.append({"kind": "table", "row": row, "text": None, "order": int(table_order)})
                document_items.sort(key=lambda item: (item.get("order") is None, item.get("order") or 0, item["kind"] != "text"))
                for item in document_items:
                    if item["kind"] == "text":
                        text = item["text"]
                        if text is None:
                            text, _text_truncated = self._text_for_row(item["row"], limit=MAX_FILE_TEXT_PREVIEW_CHARS)
                        pending_text.append((item["row"], str(text), int(item["order"])))
                    else:
                        flush_docx_text()
                        add_table(blocks, item["row"], order=int(item["order"]))
                flush_docx_text()
                if blocks:
                    sections.append({"sectionId": "document", "kind": "document", "label": "Document", "pageNumber": None, "sheetName": None, "blocks": blocks})
            elif fmt in {"xls", "xlsx"}:
                def sheet_order(row: Mapping[str, Any]) -> tuple[Any, ...]:
                    metadata = self._metadata_for_row(row)
                    raw_sheet_index = metadata.get("sheet_index")
                    try:
                        sheet_index = int(raw_sheet_index)
                    except (TypeError, ValueError):
                        sheet_index = 2**31 - 1
                    source_range = metadata.get("source_range")
                    if not isinstance(source_range, Mapping):
                        source_range = {}
                    try:
                        row_start = int(source_range.get("row_start", 0))
                    except (TypeError, ValueError):
                        row_start = 0
                    try:
                        column_start = int(source_range.get("column_start", 0))
                    except (TypeError, ValueError):
                        column_start = 0
                    return (
                        sheet_index,
                        row_start,
                        column_start,
                        str(row.get("sheet_name") or "Sheet"),
                        str(row.get("asset_id") or ""),
                    )

                ordered_table_rows = sorted(table_rows, key=sheet_order)
                sheet_names = list(dict.fromkeys(str(row.get("sheet_name") or "Sheet") for row in ordered_table_rows))
                for sheet_name in sheet_names:
                    blocks = []
                    for row in ordered_table_rows:
                        if str(row.get("sheet_name") or "Sheet") == sheet_name:
                            add_table(blocks, row)
                    if blocks:
                        sections.append({"sectionId": f"sheet-{sheet_name}", "kind": "sheet", "label": sheet_name, "pageNumber": None, "sheetName": sheet_name, "blocks": blocks})
            elif fmt in {"jpeg", "png"}:
                blocks = []
                pending_text: list[Mapping[str, Any]] = []

                def flush_image_text() -> None:
                    if not pending_text:
                        return
                    first = pending_text[0]
                    combined = _join_spatial_text(pending_text)
                    pending_text.clear()
                    add_text(blocks, first["row"], text=combined)

                for item in self._spatial_content_items(text_rows, table_rows):
                    if item["kind"] == "text":
                        pending_text.append(item)
                    else:
                        flush_image_text()
                        add_table(blocks, item["row"])
                flush_image_text()
                if blocks:
                    sections.append({"sectionId": "image", "kind": "image", "label": "Image", "pageNumber": None, "sheetName": None, "blocks": blocks})
            elif fmt == "txt":
                blocks = []
                for row in text_rows:
                    add_text(blocks, row)
                if blocks:
                    sections.append({"sectionId": "text", "kind": "text", "label": "Text", "pageNumber": None, "sheetName": None, "blocks": blocks})
            else:
                blocks = []
                for row in text_rows:
                    add_text(blocks, row)
                for row in table_rows:
                    add_table(blocks, row)
                if blocks:
                    sections.append({"sectionId": "file", "kind": "file", "label": "File", "pageNumber": None, "sheetName": None, "blocks": blocks})

            block_count = sum(len(section["blocks"]) for section in sections)
            response = {
                "file": snapshot["file"],
                "source": {
                    "fileId": file_id,
                    "relativePath": file_row.get("relative_path"),
                    "sha256": file_row.get("sha256"),
                    "root": file_row.get("source_root"),
                    "format": file_row.get("business_format"),
                },
                "sourcePreview": self._source_preview_descriptor(file_row, file_id),
                "navigation": navigation,
                "sections": sections,
                "limits": {
                    "maxTextChars": MAX_FILE_TEXT_PREVIEW_CHARS,
                    "maxTableRows": MAX_FILE_CONTENT_TABLE_ROWS,
                    "maxTableColumns": MAX_FILE_CONTENT_TABLE_COLUMNS,
                    "maxTableCellChars": MAX_FILE_CONTENT_CELL_CHARS,
                    "maxTableAssets": MAX_FILE_CONTENT_TABLES,
                    "maxBlocks": MAX_FILE_CONTENT_BLOCKS,
                },
                "truncated": content_truncated or block_count >= MAX_FILE_CONTENT_BLOCKS or len(table_rows) > MAX_FILE_CONTENT_TABLES,
                "timings": {
                    "query_ms": round(query_ms, 3),
                    "total_ms": round((time.perf_counter_ns() - started) / 1_000_000, 3),
                },
            }
            materialized = _json_value(response)
            with self._content_cache_lock:
                self._content_cache[cache_key] = copy.deepcopy(materialized)
                while len(self._content_cache) > 16:
                    self._content_cache.pop(next(iter(self._content_cache)))
            return materialized
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
            "parserValidity": (metadata or {}).get("parser_validity") if isinstance(metadata, Mapping) else None,
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
        metadata = self._metadata_for_row(row)
        presentation_columns: list[str] | None = None
        if layer == "raw":
            try:
                total = int(pl.scan_parquet(str(path)).select(pl.len()).collect().item())
            except Exception:  # pragma: no cover - defensive for a damaged preview artifact
                total = None
            mappings = metadata.get("column_mapping")
            mapped_headers: list[str | None] = []
            if isinstance(mappings, list):
                for index, column in enumerate(frame.columns):
                    mapping = mappings[index] if index < len(mappings) and isinstance(mappings[index], Mapping) else {}
                    original = mapping.get("original")
                    mapped_headers.append(str(original).strip() if original is not None and str(original).strip() else None)
            metadata_headers = metadata.get("headers")
            if isinstance(metadata_headers, list) and len(metadata_headers) >= len(frame.columns):
                mapped_headers = [
                    str(metadata_headers[index]).strip() if metadata_headers[index] is not None and str(metadata_headers[index]).strip() else None
                    for index in range(len(frame.columns))
                ]
            if any(mapped_headers):
                presentation_columns = [mapped or column for mapped, column in zip(mapped_headers, frame.columns)]
        return _json_value(
            {
                "assetId": asset_id,
                "assetType": "table",
                "layer": layer,
                "columns": frame.columns,
                "rows": records,
                "presentationColumns": presentation_columns,
                "presentation": metadata.get("presentation"),
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
