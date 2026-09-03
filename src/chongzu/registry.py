"""Versioned DuckDB registry and its single-writer operations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

import duckdb

from . import paths
from .locking import FileLock, registry_connection_open_mutex
from .processing_policy import ProcessingPlan, RegistryFileInfo, plan_processing
from .types import ExistingFile, FileOutcome, ScanSummary


class RegistryError(RuntimeError):
    """Raised when the registry cannot be initialized or updated safely."""


REGISTRY_BUSY_MARKERS = (
    "write-write conflict",
    "transaction conflict",
    "database is locked",
    "different configuration than existing connections",
    "can't open a connection to same database file",
    "cannot open file",
    "another process",
    "another program is using",
    "registry is busy",
)
REGISTRY_CONNECTION_RETRY_SECONDS = 5.0
REGISTRY_CONNECTION_RETRY_INTERVAL_SECONDS = 0.05


def is_registry_busy_error(error: BaseException | str) -> bool:
    """Classify transient DuckDB connection/transaction contention safely."""

    detail = str(error).casefold()
    exception_name = type(error).__name__.casefold() if not isinstance(error, str) else ""
    return exception_name in {
        "transactionexception",
        "concurrentmodificationexception",
    } or any(marker in detail for marker in REGISTRY_BUSY_MARKERS)


def _connect_registry(path: Path, *, read_only: bool) -> duckdb.DuckDBPyConnection:
    """Open one consistently configured connection across short Windows IO contention."""

    deadline = time.monotonic() + REGISTRY_CONNECTION_RETRY_SECONDS
    while True:
        try:
            with registry_connection_open_mutex(path):
                return duckdb.connect(str(path), read_only=read_only)
        except Exception as exc:
            if not is_registry_busy_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(REGISTRY_CONNECTION_RETRY_INTERVAL_SECONDS)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def canonical_source_root(path: Path | str, *, require_directory: bool = False) -> str:
    resolved = Path(path).expanduser().resolve(strict=require_directory)
    if require_directory and not resolved.is_dir():
        raise ValueError(f"Source root is not a directory: {resolved}")
    # normcase makes repeated scans stable when a Windows caller changes drive
    # or directory-name casing; it does not alter the absolute identity.
    return os.path.normcase(os.path.abspath(os.fspath(resolved)))


def file_id_for(source_root: str, relative_path: str) -> str:
    """Create a stable path-instance ID; it is not a content identity."""

    identity = f"{source_root.casefold()}\0{relative_path.casefold()}"
    return uuid5(NAMESPACE_URL, identity).hex


CORE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS scan_runs (
        run_id VARCHAR PRIMARY KEY,
        source_root VARCHAR NOT NULL,
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP,
        status VARCHAR NOT NULL,
        discovered_count BIGINT NOT NULL DEFAULT 0,
        hashed_count BIGINT NOT NULL DEFAULT 0,
        reused_hash_count BIGINT NOT NULL DEFAULT 0,
        new_count BIGINT NOT NULL DEFAULT 0,
        changed_count BIGINT NOT NULL DEFAULT 0,
        unchanged_count BIGINT NOT NULL DEFAULT 0,
        missing_count BIGINT NOT NULL DEFAULT 0,
        failed_count BIGINT NOT NULL DEFAULT 0,
        discovery_error_count BIGINT NOT NULL DEFAULT 0,
        total_bytes BIGINT NOT NULL DEFAULT 0,
        exact_duplicate_paths BIGINT NOT NULL DEFAULT 0,
        elapsed_ms DOUBLE,
        hashing_ms DOUBLE NOT NULL DEFAULT 0,
        detection_ms DOUBLE NOT NULL DEFAULT 0,
        registry_write_ms DOUBLE NOT NULL DEFAULT 0,
        log_path VARCHAR,
        pipeline_version VARCHAR NOT NULL,
        schema_version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        file_id VARCHAR PRIMARY KEY,
        source_root VARCHAR NOT NULL,
        relative_path VARCHAR NOT NULL,
        filename VARCHAR NOT NULL,
        observed_extension VARCHAR NOT NULL,
        size_bytes BIGINT,
        mtime_ns BIGINT,
        sha256 VARCHAR,
        detected_type VARCHAR NOT NULL,
        mime_like_type VARCHAR NOT NULL,
        detection_method VARCHAR NOT NULL,
        detection_confidence VARCHAR NOT NULL,
        routing_class VARCHAR NOT NULL,
        support_status VARCHAR NOT NULL,
        business_format VARCHAR,
        table_candidate BOOLEAN NOT NULL,
        text_candidate BOOLEAN NOT NULL,
        may_require_ocr BOOLEAN NOT NULL,
        may_require_visual_processing BOOLEAN NOT NULL,
        policy_reason VARCHAR NOT NULL,
        current_presence_state VARCHAR NOT NULL,
        first_seen_run VARCHAR NOT NULL,
        last_seen_run VARCHAR NOT NULL,
        last_changed_run VARCHAR,
        latest_error_code VARCHAR,
        latest_error_message VARCHAR,
        last_fingerprint_ms DOUBLE,
        last_detection_ms DOUBLE,
        updated_at TIMESTAMP NOT NULL,
        UNIQUE(source_root, relative_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contents (
        sha256 VARCHAR PRIMARY KEY,
        size_bytes BIGINT,
        first_seen_run VARCHAR NOT NULL,
        last_seen_run VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_attempts (
        run_id VARCHAR NOT NULL,
        file_id VARCHAR NOT NULL,
        source_root VARCHAR NOT NULL,
        relative_path VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        hash_reused BOOLEAN NOT NULL,
        size_before_bytes BIGINT,
        size_after_bytes BIGINT,
        mtime_before_ns BIGINT,
        mtime_after_ns BIGINT,
        sha256 VARCHAR,
        detected_type VARCHAR NOT NULL,
        mime_like_type VARCHAR NOT NULL,
        detection_method VARCHAR NOT NULL,
        detection_confidence VARCHAR NOT NULL,
        routing_class VARCHAR NOT NULL,
        error_code VARCHAR,
        error_message VARCHAR,
        fingerprint_ms DOUBLE,
        detection_ms DOUBLE,
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP NOT NULL,
        PRIMARY KEY(run_id, file_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_errors (
        run_id VARCHAR NOT NULL,
        path VARCHAR,
        error_code VARCHAR NOT NULL,
        error_message VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_files_source_path ON files(source_root, relative_path)",
    "CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256)",
    "CREATE INDEX IF NOT EXISTS idx_files_presence ON files(source_root, current_presence_state)",
    "CREATE INDEX IF NOT EXISTS idx_files_support ON files(support_status, business_format)",
)


CATALOG_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS extraction_runs (
        extraction_run_id VARCHAR PRIMARY KEY,
        file_id VARCHAR NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        extraction_identity VARCHAR NOT NULL,
        source_root VARCHAR NOT NULL,
        source_relative_path VARCHAR NOT NULL,
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP,
        status VARCHAR NOT NULL,
        force BOOLEAN NOT NULL DEFAULT FALSE,
        pipeline_version VARCHAR NOT NULL,
        configuration_version VARCHAR NOT NULL,
        attempted_route VARCHAR NOT NULL,
        route_reason VARCHAR NOT NULL,
        timings_json JSON,
        warnings_json JSON,
        extractor_versions_json JSON,
        table_count BIGINT NOT NULL DEFAULT 0,
        sheet_count BIGINT NOT NULL DEFAULT 0,
        quality_issue_count BIGINT NOT NULL DEFAULT 0,
        total_rows BIGINT NOT NULL DEFAULT 0,
        total_bytes BIGINT NOT NULL DEFAULT 0,
        error_category VARCHAR,
        error_message VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS table_assets (
        table_id VARCHAR PRIMARY KEY,
        file_id VARCHAR NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        extraction_run_id VARCHAR NOT NULL,
        extractor VARCHAR NOT NULL,
        extractor_version VARCHAR NOT NULL,
        source_kind VARCHAR NOT NULL,
        source_relative_path VARCHAR NOT NULL,
        sheet_name VARCHAR,
        page_number INTEGER,
        bbox_json JSON,
        source_row_start BIGINT NOT NULL,
        source_row_end BIGINT NOT NULL,
        source_column_start BIGINT NOT NULL,
        source_column_end BIGINT NOT NULL,
        row_count BIGINT NOT NULL,
        column_count BIGINT NOT NULL,
        columns_json JSON NOT NULL,
        raw_artifact_path VARCHAR NOT NULL,
        normalized_artifact_path VARCHAR,
        metadata_artifact_path VARCHAR NOT NULL,
        extraction_confidence DOUBLE,
        quality_status VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL
        ,is_current BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS text_assets (
        text_asset_id VARCHAR PRIMARY KEY,
        file_id VARCHAR NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        extraction_run_id VARCHAR NOT NULL,
        extractor VARCHAR NOT NULL,
        extractor_version VARCHAR NOT NULL,
        source_kind VARCHAR NOT NULL,
        page_number INTEGER,
        section VARCHAR,
        bbox_json JSON,
        text VARCHAR NOT NULL,
        language VARCHAR,
        created_at TIMESTAMP NOT NULL,
        source_relative_path VARCHAR,
        raw_artifact_path VARCHAR,
        normalized_artifact_path VARCHAR,
        metadata_artifact_path VARCHAR,
        is_current BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS text_chunks (
        chunk_id VARCHAR PRIMARY KEY,
        text_asset_id VARCHAR NOT NULL,
        file_id VARCHAR NOT NULL,
        chunk_index INTEGER NOT NULL,
        text VARCHAR NOT NULL,
        char_start BIGINT NOT NULL,
        char_end BIGINT NOT NULL,
        provenance_json JSON NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS semantic_metadata (
        asset_id VARCHAR NOT NULL,
        asset_type VARCHAR NOT NULL,
        display_name VARCHAR NOT NULL,
        category VARCHAR NOT NULL,
        description VARCHAR NOT NULL,
        keywords_json JSON NOT NULL,
        summary VARCHAR NOT NULL,
        semantic_fields_json JSON NOT NULL,
        model VARCHAR NOT NULL,
        prompt_version VARCHAR NOT NULL,
        confidence DOUBLE,
        generated_at TIMESTAMP NOT NULL,
        semantic_run_id VARCHAR,
        input_hash VARCHAR NOT NULL DEFAULT '',
        current BOOLEAN NOT NULL DEFAULT TRUE,
        PRIMARY KEY(asset_id, asset_type, model, prompt_version, generated_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS semantic_runs (
        semantic_run_id VARCHAR PRIMARY KEY,
        semantic_identity VARCHAR NOT NULL,
        asset_id VARCHAR NOT NULL,
        asset_type VARCHAR NOT NULL CHECK (asset_type IN ('table', 'text')),
        file_id VARCHAR NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        normalized_artifact_identity VARCHAR NOT NULL,
        model VARCHAR NOT NULL,
        prompt_version VARCHAR NOT NULL,
        config_version VARCHAR NOT NULL,
        input_hash VARCHAR NOT NULL,
        provider VARCHAR NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('running', 'successful', 'failed', 'reused', 'skipped')),
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP,
        current BOOLEAN NOT NULL DEFAULT FALSE,
        input_metadata_json JSON,
        warnings_json JSON,
        error_code VARCHAR,
        error_message VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quality_issues (
        issue_id VARCHAR PRIMARY KEY,
        extraction_run_id VARCHAR,
        cleaning_run_id VARCHAR,
        semantic_run_id VARCHAR,
        asset_id VARCHAR NOT NULL,
        severity VARCHAR NOT NULL,
        issue_type VARCHAR NOT NULL,
        description VARCHAR NOT NULL,
        evidence_json JSON NOT NULL,
        detected_by VARCHAR NOT NULL,
        suggested_action VARCHAR NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('open', 'accepted', 'ignored', 'resolved')),
        created_at TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_table_assets_file ON table_assets(file_id, content_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_text_assets_file ON text_assets(file_id, content_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_text_chunks_asset ON text_chunks(text_asset_id, chunk_index)",
    "CREATE INDEX IF NOT EXISTS idx_semantic_metadata_asset ON semantic_metadata(asset_id, asset_type)",
    "CREATE INDEX IF NOT EXISTS idx_semantic_metadata_current ON semantic_metadata(asset_id, asset_type, current)",
    "CREATE INDEX IF NOT EXISTS idx_semantic_runs_identity ON semantic_runs(semantic_identity, status)",
    "CREATE INDEX IF NOT EXISTS idx_semantic_runs_asset ON semantic_runs(asset_id, asset_type, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_quality_issues_asset ON quality_issues(asset_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_quality_issues_cleaning ON quality_issues(cleaning_run_id, asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_quality_issues_semantic ON quality_issues(semantic_run_id, asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_extraction_runs_file ON extraction_runs(file_id, content_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_extraction_identity ON extraction_runs(extraction_identity, status)",
)


CLEANING_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS cleaning_runs (
        cleaning_run_id VARCHAR PRIMARY KEY,
        cleaning_identity VARCHAR NOT NULL,
        asset_id VARCHAR NOT NULL,
        asset_type VARCHAR NOT NULL CHECK (asset_type IN ('table', 'text')),
        file_id VARCHAR NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        raw_artifact_identity VARCHAR NOT NULL,
        cleaner VARCHAR NOT NULL,
        cleaner_version VARCHAR NOT NULL,
        config_version VARCHAR NOT NULL,
        source_root VARCHAR NOT NULL,
        source_relative_path VARCHAR NOT NULL,
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP,
        status VARCHAR NOT NULL CHECK (status IN ('running', 'successful', 'failed', 'interrupted')),
        force BOOLEAN NOT NULL DEFAULT FALSE,
        normalized_artifact_path VARCHAR,
        manifest_artifact_path VARCHAR,
        profile_artifact_path VARCHAR,
        profile_json JSON,
        timings_json JSON,
        quality_status VARCHAR NOT NULL DEFAULT 'needs_review',
        warnings_json JSON,
        error_category VARCHAR,
        error_message VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS table_profiles (
        profile_id VARCHAR PRIMARY KEY,
        table_id VARCHAR NOT NULL,
        cleaning_run_id VARCHAR NOT NULL,
        cleaning_identity VARCHAR NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        row_count BIGINT NOT NULL,
        column_count BIGINT NOT NULL,
        null_count BIGINT NOT NULL,
        null_ratio DOUBLE NOT NULL,
        empty_row_count_before BIGINT NOT NULL,
        empty_column_count_before BIGINT NOT NULL,
        exact_duplicate_row_count BIGINT NOT NULL,
        empty_cell_ratio DOUBLE NOT NULL,
        long_text_cell_ratio DOUBLE NOT NULL,
        irregular_row_width BOOLEAN NOT NULL,
        ocr_mean_confidence DOUBLE,
        ocr_min_confidence DOUBLE,
        provenance_complete BOOLEAN NOT NULL,
        profile_json JSON NOT NULL,
        profile_artifact_path VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS text_profiles (
        profile_id VARCHAR PRIMARY KEY,
        text_asset_id VARCHAR NOT NULL,
        cleaning_run_id VARCHAR NOT NULL,
        cleaning_identity VARCHAR NOT NULL,
        content_sha256 VARCHAR NOT NULL,
        char_count BIGINT NOT NULL,
        line_count BIGINT NOT NULL,
        page_count BIGINT NOT NULL,
        block_count BIGINT NOT NULL,
        chunk_count BIGINT NOT NULL,
        language_hint VARCHAR,
        extraction_source VARCHAR NOT NULL,
        ocr_mean_confidence DOUBLE,
        empty_content BOOLEAN NOT NULL,
        low_content BOOLEAN NOT NULL,
        profile_json JSON NOT NULL,
        profile_artifact_path VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cleaning_runs_asset ON cleaning_runs(asset_id, content_sha256, finished_at)",
    "CREATE INDEX IF NOT EXISTS idx_cleaning_identity ON cleaning_runs(cleaning_identity, status)",
    "CREATE INDEX IF NOT EXISTS idx_table_profiles_asset ON table_profiles(table_id, cleaning_identity)",
    "CREATE INDEX IF NOT EXISTS idx_text_profiles_asset ON text_profiles(text_asset_id, cleaning_identity)",
)


CATALOG_VIEW_STATEMENT = """
CREATE OR REPLACE VIEW catalog_assets AS
WITH latest_cleaning AS (
    SELECT * EXCLUDE (row_number)
    FROM (
        SELECT c.*,
               ROW_NUMBER() OVER (
                   PARTITION BY c.asset_id, c.content_sha256
                   ORDER BY c.finished_at DESC NULLS LAST, c.started_at DESC, c.cleaning_run_id DESC
               ) AS row_number
        FROM cleaning_runs c
    ) ranked
    WHERE row_number = 1
),
issue_counts AS (
    SELECT q.asset_id,
           COUNT(*) FILTER (WHERE q.status IN ('open', 'accepted')) AS issue_count
    FROM quality_issues q
    LEFT JOIN latest_cleaning c ON c.cleaning_run_id = q.cleaning_run_id
    WHERE q.issue_type <> 'possible_table_candidate'
      AND (q.cleaning_run_id IS NULL OR c.cleaning_run_id IS NOT NULL)
    GROUP BY q.asset_id
),
semantic_current AS (
    SELECT * EXCLUDE (row_number)
    FROM (
        SELECT s.*,
               ROW_NUMBER() OVER (
                   PARTITION BY s.asset_id, s.asset_type
                   ORDER BY s.current DESC, s.generated_at DESC, s.semantic_run_id DESC NULLS LAST
               ) AS row_number
        FROM semantic_metadata s
        WHERE s.current = TRUE
    ) ranked
    WHERE row_number = 1
),
text_chunk_counts AS (
    SELECT text_asset_id, COUNT(*) AS chunk_count
    FROM text_chunks
    GROUP BY text_asset_id
),
table_catalog AS (
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
            WHEN t.source_kind = 'image' THEN f.filename || ' / Table ' || CAST(ROW_NUMBER() OVER (PARTITION BY t.file_id ORDER BY t.table_id) AS VARCHAR)
            ELSE f.filename
        END AS fallback_display_name,
        s.display_name AS semantic_display_name,
        COALESCE(s.display_name,
            CASE
                WHEN t.sheet_name IS NOT NULL THEN f.filename || ' / ' || t.sheet_name
                WHEN t.page_number IS NOT NULL THEN f.filename || ' / Page ' || CAST(t.page_number AS VARCHAR) || ' / Table ' || CAST(ROW_NUMBER() OVER (PARTITION BY t.file_id, t.page_number ORDER BY t.table_id) AS VARCHAR)
                WHEN t.source_kind = 'image' THEN f.filename || ' / Table ' || CAST(ROW_NUMBER() OVER (PARTITION BY t.file_id ORDER BY t.table_id) AS VARCHAR)
                ELSE f.filename
            END) AS effective_display_name,
        s.category AS category,
        COALESCE(p.row_count, t.row_count) AS "rows",
        CAST(NULL AS BIGINT) AS "chars",
        COALESCE(p.column_count, t.column_count) AS "columns",
        CAST(NULL AS BIGINT) AS chunks,
         COALESCE(c.quality_status,
            CASE
                WHEN f.business_format = 'pdf' OR t.source_kind IN ('page', 'image') OR LOWER(t.extractor) LIKE '%img2table%' THEN 'needs_review'
                WHEN t.quality_status = 'pass' THEN 'ready'
                WHEN t.quality_status = 'fail' THEN 'unusable'
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
    JOIN files f ON f.file_id = t.file_id
    LEFT JOIN latest_cleaning c ON c.asset_id = t.table_id AND c.content_sha256 = t.content_sha256
    LEFT JOIN table_profiles p ON p.cleaning_run_id = c.cleaning_run_id AND p.table_id = t.table_id
    LEFT JOIN issue_counts i ON i.asset_id = t.table_id
    LEFT JOIN semantic_current s ON s.asset_id = t.table_id AND s.asset_type = 'table'
    WHERE t.is_current = TRUE AND f.current_presence_state = 'present'
),
text_catalog AS (
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
        s.category AS category,
        CAST(NULL AS BIGINT) AS "rows",
        COALESCE(p.char_count, LENGTH(t.text)) AS "chars",
        CAST(NULL AS BIGINT) AS "columns",
            COALESCE(p.chunk_count, tc.chunk_count, 0) AS chunks,
        COALESCE(c.quality_status,
            CASE
                WHEN LENGTH(TRIM(t.text)) = 0 THEN 'unusable'
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
    JOIN files f ON f.file_id = t.file_id
    LEFT JOIN latest_cleaning c ON c.asset_id = t.text_asset_id AND c.content_sha256 = t.content_sha256
        LEFT JOIN text_profiles p ON p.cleaning_run_id = c.cleaning_run_id AND p.text_asset_id = t.text_asset_id
        LEFT JOIN text_chunk_counts tc ON tc.text_asset_id = t.text_asset_id
    LEFT JOIN issue_counts i ON i.asset_id = t.text_asset_id
    LEFT JOIN semantic_current s ON s.asset_id = t.text_asset_id AND s.asset_type = 'text'
    WHERE t.is_current = TRUE AND f.current_presence_state = 'present'
)
SELECT * FROM table_catalog
UNION ALL
SELECT * FROM text_catalog
"""


SCHEMA_STATEMENTS = CORE_SCHEMA_STATEMENTS + CATALOG_SCHEMA_STATEMENTS + CLEANING_SCHEMA_STATEMENTS + (CATALOG_VIEW_STATEMENT,)


# DuckDB secondary indexes can be left inconsistent if Windows terminates a
# process in the middle of a write transaction.  These are the only indexes
# touched by the startup run-state recovery below.  If recovery encounters
# that driver-level fatal error, the connection is reopened and the affected
# table indexes are rebuilt before retrying the state update.  This is a
# repair of index metadata only; rows and derived artifacts are not removed.
_RECOVERY_INDEX_STATEMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "extraction_runs": (
        ("idx_extraction_runs_file", "CREATE INDEX idx_extraction_runs_file ON extraction_runs(file_id, content_sha256)"),
        ("idx_extraction_identity", "CREATE INDEX idx_extraction_identity ON extraction_runs(extraction_identity, status)"),
    ),
    "cleaning_runs": (
        ("idx_cleaning_runs_asset", "CREATE INDEX idx_cleaning_runs_asset ON cleaning_runs(asset_id, content_sha256, finished_at)"),
        ("idx_cleaning_identity", "CREATE INDEX idx_cleaning_identity ON cleaning_runs(cleaning_identity, status)"),
    ),
    "semantic_runs": (
        ("idx_semantic_runs_identity", "CREATE INDEX idx_semantic_runs_identity ON semantic_runs(semantic_identity, status)"),
        ("idx_semantic_runs_asset", "CREATE INDEX idx_semantic_runs_asset ON semantic_runs(asset_id, asset_type, started_at)"),
    ),
}


def _processing_plan(
    *, file_id: str, detected_type: str, mime_like_type: str, observed_extension: str, routing_class: str
) -> ProcessingPlan:
    return plan_processing(
        RegistryFileInfo(
            file_id=file_id,
            detected_type=detected_type,
            mime_like_type=mime_like_type,
            observed_extension=observed_extension,
            routing_class=routing_class,
        )
    )


def _migrate_v1_to_v2(connection: duckdb.DuckDBPyConnection) -> None:
    # DuckDB does not support mixing ALTER TABLE and later updates to that table
    # in one explicit transaction. Column/table creation is therefore
    # idempotent and restartable; the policy backfill and metadata version bump
    # remain atomic so a partial migration never advertises completion of v2.
    columns = {row[1] for row in connection.execute("PRAGMA table_info('files')").fetchall()}
    additions = {
        "support_status": "ALTER TABLE files ADD COLUMN support_status VARCHAR DEFAULT 'unsupported'",
        "business_format": "ALTER TABLE files ADD COLUMN business_format VARCHAR",
        "table_candidate": "ALTER TABLE files ADD COLUMN table_candidate BOOLEAN DEFAULT FALSE",
        "text_candidate": "ALTER TABLE files ADD COLUMN text_candidate BOOLEAN DEFAULT FALSE",
        "may_require_ocr": "ALTER TABLE files ADD COLUMN may_require_ocr BOOLEAN DEFAULT FALSE",
        "may_require_visual_processing": "ALTER TABLE files ADD COLUMN may_require_visual_processing BOOLEAN DEFAULT FALSE",
        "policy_reason": "ALTER TABLE files ADD COLUMN policy_reason VARCHAR DEFAULT 'migration_pending'",
    }
    for name, statement in additions.items():
        if name not in columns:
            connection.execute(statement)
    for statement in CATALOG_SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_files_support ON files(support_status, business_format)")

    connection.execute("BEGIN TRANSACTION")
    try:
        rows = connection.execute(
            "SELECT file_id, detected_type, mime_like_type, observed_extension, routing_class FROM files"
        ).fetchall()
        for file_id, detected_type, mime_like_type, observed_extension, routing_class in rows:
            plan = _processing_plan(
                file_id=file_id,
                detected_type=detected_type,
                mime_like_type=mime_like_type,
                observed_extension=observed_extension,
                routing_class=routing_class,
            )
            connection.execute(
                """
                UPDATE files SET support_status=?, business_format=?, table_candidate=?, text_candidate=?,
                    may_require_ocr=?, may_require_visual_processing=?, policy_reason=? WHERE file_id=?
                """,
                [
                    plan.support_status.value,
                    plan.business_format.value if plan.business_format else None,
                    plan.attempt_table_extraction,
                    plan.attempt_text_extraction,
                    plan.may_require_ocr,
                    plan.may_require_visual_processing,
                    plan.reason_code,
                    file_id,
                ],
            )
        connection.execute(
            "UPDATE registry_meta SET meta_value=?, updated_at=? WHERE meta_key=?",
            ["2", utc_now(), paths.REGISTRY_SCHEMA_NAME],
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_v2_to_v3(connection: duckdb.DuckDBPyConnection) -> None:
    additions: dict[str, dict[str, str]] = {
        "extraction_runs": {
            "extraction_identity": "ALTER TABLE extraction_runs ADD COLUMN extraction_identity VARCHAR DEFAULT ''",
            "source_root": "ALTER TABLE extraction_runs ADD COLUMN source_root VARCHAR DEFAULT ''",
            "source_relative_path": "ALTER TABLE extraction_runs ADD COLUMN source_relative_path VARCHAR DEFAULT ''",
            "force": "ALTER TABLE extraction_runs ADD COLUMN force BOOLEAN DEFAULT FALSE",
            "table_count": "ALTER TABLE extraction_runs ADD COLUMN table_count BIGINT DEFAULT 0",
            "sheet_count": "ALTER TABLE extraction_runs ADD COLUMN sheet_count BIGINT DEFAULT 0",
            "quality_issue_count": "ALTER TABLE extraction_runs ADD COLUMN quality_issue_count BIGINT DEFAULT 0",
            "total_rows": "ALTER TABLE extraction_runs ADD COLUMN total_rows BIGINT DEFAULT 0",
            "total_bytes": "ALTER TABLE extraction_runs ADD COLUMN total_bytes BIGINT DEFAULT 0",
        },
        "table_assets": {
            "source_relative_path": "ALTER TABLE table_assets ADD COLUMN source_relative_path VARCHAR DEFAULT ''",
            "source_row_start": "ALTER TABLE table_assets ADD COLUMN source_row_start BIGINT DEFAULT 0",
            "source_row_end": "ALTER TABLE table_assets ADD COLUMN source_row_end BIGINT DEFAULT 0",
            "source_column_start": "ALTER TABLE table_assets ADD COLUMN source_column_start BIGINT DEFAULT 0",
            "source_column_end": "ALTER TABLE table_assets ADD COLUMN source_column_end BIGINT DEFAULT 0",
            "metadata_artifact_path": "ALTER TABLE table_assets ADD COLUMN metadata_artifact_path VARCHAR DEFAULT ''",
            "is_current": "ALTER TABLE table_assets ADD COLUMN is_current BOOLEAN DEFAULT TRUE",
        },
        "text_assets": {
            "source_relative_path": "ALTER TABLE text_assets ADD COLUMN source_relative_path VARCHAR",
            "raw_artifact_path": "ALTER TABLE text_assets ADD COLUMN raw_artifact_path VARCHAR",
            "normalized_artifact_path": "ALTER TABLE text_assets ADD COLUMN normalized_artifact_path VARCHAR",
            "metadata_artifact_path": "ALTER TABLE text_assets ADD COLUMN metadata_artifact_path VARCHAR",
            "is_current": "ALTER TABLE text_assets ADD COLUMN is_current BOOLEAN DEFAULT TRUE",
        },
        "quality_issues": {
            "extraction_run_id": "ALTER TABLE quality_issues ADD COLUMN extraction_run_id VARCHAR",
            "created_at": "ALTER TABLE quality_issues ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        },
    }
    for table, table_additions in additions.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()}
        for name, statement in table_additions.items():
            if name not in columns:
                connection.execute(statement)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_extraction_identity ON extraction_runs(extraction_identity, status)")
    connection.execute(
        "UPDATE registry_meta SET meta_value=?, updated_at=? WHERE meta_key=?",
        ["3", utc_now(), paths.REGISTRY_SCHEMA_NAME],
    )


def _migrate_v3_to_v4(connection: duckdb.DuckDBPyConnection) -> None:
    """Add cleaning/profile/catalog tables without rewriting extraction history."""

    columns = {row[1] for row in connection.execute("PRAGMA table_info('quality_issues')").fetchall()}
    if "cleaning_run_id" not in columns:
        connection.execute("ALTER TABLE quality_issues ADD COLUMN cleaning_run_id VARCHAR")
    for statement in CLEANING_SCHEMA_STATEMENTS:
        connection.execute(statement)
    _ensure_v5_semantic_schema(connection)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_quality_issues_cleaning ON quality_issues(cleaning_run_id, asset_id)")
    connection.execute(CATALOG_VIEW_STATEMENT)
    connection.execute(
        "UPDATE registry_meta SET meta_value=?, updated_at=? WHERE meta_key=?",
        ["4", utc_now(), paths.REGISTRY_SCHEMA_NAME],
    )


def _ensure_v5_semantic_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Add Phase 7 semantic history columns/tables without rewriting assets."""

    semantic_columns = {row[1] for row in connection.execute("PRAGMA table_info('semantic_metadata')").fetchall()}
    additions = {
        "semantic_run_id": "ALTER TABLE semantic_metadata ADD COLUMN semantic_run_id VARCHAR",
        "input_hash": "ALTER TABLE semantic_metadata ADD COLUMN input_hash VARCHAR DEFAULT ''",
        "current": "ALTER TABLE semantic_metadata ADD COLUMN current BOOLEAN DEFAULT TRUE",
    }
    for name, statement in additions.items():
        if name not in semantic_columns:
            connection.execute(statement)
    issue_columns = {row[1] for row in connection.execute("PRAGMA table_info('quality_issues')").fetchall()}
    if "semantic_run_id" not in issue_columns:
        connection.execute("ALTER TABLE quality_issues ADD COLUMN semantic_run_id VARCHAR")
    for statement in CATALOG_SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_semantic_metadata_current ON semantic_metadata(asset_id, asset_type, current)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_semantic_runs_identity ON semantic_runs(semantic_identity, status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_semantic_runs_asset ON semantic_runs(asset_id, asset_type, started_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_quality_issues_semantic ON quality_issues(semantic_run_id, asset_id)")


def _migrate_v4_to_v5(connection: duckdb.DuckDBPyConnection) -> None:
    """Add semantic runs/history and rebuild the unified catalog view."""

    _ensure_v5_semantic_schema(connection)
    connection.execute(CATALOG_VIEW_STATEMENT)
    connection.execute(
        "UPDATE registry_meta SET meta_value=?, updated_at=? WHERE meta_key=?",
        ["5", utc_now(), paths.REGISTRY_SCHEMA_NAME],
    )


def initialize_schema(connection: duckdb.DuckDBPyConnection) -> None:
    # The current-schema path is deliberately read-only. DuckDB treats
    # CREATE OR REPLACE VIEW as a catalog write, so it cannot be used as a
    # harmless health check while readers and a processing writer are active.
    try:
        row = connection.execute(
            "SELECT meta_value FROM registry_meta WHERE meta_key = ?",
            [paths.REGISTRY_SCHEMA_NAME],
        ).fetchone()
    except duckdb.CatalogException:
        row = None
    if row is None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS registry_meta (
                meta_key VARCHAR PRIMARY KEY,
                meta_value VARCHAR NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            """
        )
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO registry_meta VALUES (?, ?, ?)",
            [paths.REGISTRY_SCHEMA_NAME, str(paths.REGISTRY_SCHEMA_VERSION), utc_now()],
        )
        return
    version = int(row[0])
    if version > paths.REGISTRY_SCHEMA_VERSION:
        raise RegistryError(f"Registry schema {version} is newer than supported {paths.REGISTRY_SCHEMA_VERSION}")
    if version == 1 and paths.REGISTRY_SCHEMA_VERSION >= 2:
        _migrate_v1_to_v2(connection)
        version = 2
    if version == 2 and paths.REGISTRY_SCHEMA_VERSION >= 3:
        _migrate_v2_to_v3(connection)
        version = 3
    if version == 3 and paths.REGISTRY_SCHEMA_VERSION >= 4:
        _migrate_v3_to_v4(connection)
        version = 4
    if version == 4 and paths.REGISTRY_SCHEMA_VERSION >= 5:
        _migrate_v4_to_v5(connection)
        version = 5
    if version < paths.REGISTRY_SCHEMA_VERSION:
        raise RegistryError(f"Registry schema migration from {version} to {paths.REGISTRY_SCHEMA_VERSION} is not implemented")


class Registry:
    """A DuckDB connection owned by the coordinator/writer thread."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, path: Path):
        self.connection = connection
        self.path = path

    @classmethod
    def open(
        cls,
        path: Path | str | None = None,
        *,
        initialize: bool = True,
        read_only: bool = False,
    ) -> "Registry":
        """Open a Registry connection.

        The default keeps the CLI/test contract that a missing registry is
        created on first open. Normal server operations pass
        ``initialize=False``. Operational readers also pass
        ``initialize=False`` and use the same read-write-capable DuckDB
        configuration as writers; the application service boundary, rather
        than DuckDB's connection mode, prevents reader-side mutations.
        """

        if read_only and initialize:
            raise ValueError("read-only registry connections cannot initialize schema")
        registry_path = Path(path or paths.REGISTRY_PATH).resolve()
        if not read_only:
            registry_path.parent.mkdir(parents=True, exist_ok=True)
        elif not registry_path.is_file():
            raise RegistryError(f"registry does not exist: {registry_path}")
        connection: duckdb.DuckDBPyConnection | None = None
        try:
            connection = _connect_registry(registry_path, read_only=read_only)
            if initialize:
                schema_lock = FileLock(registry_path.with_name(registry_path.name + ".schema.lock"))
                schema_lock.acquire(timeout=30.0)
                try:
                    initialize_schema(connection)
                finally:
                    schema_lock.release()
        except Exception as exc:  # noqa: BLE001 - convert DB driver errors at the boundary
            if connection is not None:
                connection.close()
            raise RegistryError(f"Unable to initialize registry {registry_path}: {exc}") from exc
        if connection is None:  # pragma: no cover - defensive for a driver failure
            raise RegistryError(f"Unable to open registry {registry_path}")
        return cls(connection, registry_path)

    @classmethod
    def open_reader(cls, path: Path | str | None = None) -> "Registry":
        """Open an operational reader without schema initialization or DDL.

        Keep this connection configuration identical to ``Registry.open`` so
        one server process never mixes DuckDB read-only and read-write
        connections for the same database file.
        """

        return cls.open(path, initialize=False, read_only=False)

    @classmethod
    def ensure_initialized(cls, path: Path | str | None = None) -> None:
        """Create or explicitly migrate a registry, then close it."""

        registry = cls.open(path, initialize=True)
        registry.close()

    def close(self) -> None:
        self.connection.close()

    def schema_version(self) -> int:
        row = self.connection.execute(
            "SELECT meta_value FROM registry_meta WHERE meta_key = ?",
            [paths.REGISTRY_SCHEMA_NAME],
        ).fetchone()
        if row is None:
            raise RegistryError("registry schema metadata is missing")
        return int(row[0])

    def create_run(self, run_id: str, source_root: str, started_at: datetime, log_path: str | None) -> None:
        self.connection.execute(
            """
            INSERT INTO scan_runs(run_id, source_root, started_at, status, log_path, pipeline_version, schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [run_id, source_root, started_at, "running", log_path, paths.PIPELINE_VERSION, paths.REGISTRY_SCHEMA_VERSION],
        )

    def recover_incomplete_runs(self, source_root: str) -> int:
        """Mark prior open runs for this source as interrupted before retrying.

        File observations are durable after each coordinator write, so a later
        run can safely reuse completed observations even when a process stopped
        between files. This operation is intentionally scoped to one source.
        """

        now = utc_now()

        def recover() -> int:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM scan_runs WHERE source_root=? AND status='running'",
                [source_root],
            ).fetchone()
            count = int(row[0] or 0)
            self.connection.execute(
                """
                UPDATE scan_runs SET status='interrupted', finished_at=?
                WHERE source_root=? AND status='running'
                """,
                [now, source_root],
            )
            return count

        return self._run_recovery_with_index_repair("scan_runs", recover)

    def load_files(self, source_root: str) -> dict[str, ExistingFile]:
        rows = self.connection.execute(
            """
            SELECT file_id, relative_path, size_bytes, mtime_ns, sha256,
                   current_presence_state, first_seen_run, last_changed_run
            FROM files WHERE source_root = ?
            """,
            [source_root],
        ).fetchall()
        return {
            row[1]: ExistingFile(
                file_id=row[0],
                relative_path=row[1],
                size_bytes=row[2],
                mtime_ns=row[3],
                sha256=row[4],
                current_presence_state=row[5],
                first_seen_run=row[6],
                last_changed_run=row[7],
            )
            for row in rows
        }

    def count_present_files(self, source_root: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM files WHERE source_root=? AND current_presence_state='present'",
            [source_root],
        ).fetchone()
        return int(row[0] or 0)

    def structured_candidates(self, source_root: str) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            """
            SELECT file_id, sha256, source_root, relative_path, business_format, size_bytes, mtime_ns
            FROM files
            WHERE source_root=? AND current_presence_state='present' AND support_status='supported'
              AND table_candidate=TRUE AND business_format IN ('csv', 'tsv', 'xls', 'xlsx')
              AND sha256 IS NOT NULL
            ORDER BY relative_path
            """,
            [source_root],
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def pdf_candidates(self, source_root: str) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            """
            SELECT file_id, sha256, source_root, relative_path, business_format, size_bytes, mtime_ns
            FROM files
            WHERE source_root=? AND current_presence_state='present' AND support_status='supported'
              AND text_candidate=TRUE AND business_format='pdf' AND sha256 IS NOT NULL
            ORDER BY relative_path
            """,
            [source_root],
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def ocr_candidates(self, source_root: str) -> list[dict[str, Any]]:
        """Return image files and PDF files that may have OCR work.

        The registry remains the source of truth for routing.  PDF page-level
        selection is performed by the OCR runner from the persisted Phase 4A
        profile; this query deliberately does not infer a second PDF policy.
        """

        cursor = self.connection.execute(
            """
            SELECT file_id, sha256, source_root, relative_path, business_format,
                   size_bytes, mtime_ns, may_require_ocr
            FROM files
            WHERE source_root=? AND current_presence_state='present'
              AND support_status='supported'
              AND business_format IN ('pdf', 'jpeg', 'png')
              AND sha256 IS NOT NULL
            ORDER BY relative_path
            """,
            [source_root],
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def vision_candidates(self, source_root: str) -> list[dict[str, Any]]:
        """Return only image files eligible for the explicit Vision route."""

        cursor = self.connection.execute(
            """
            SELECT file_id, sha256, source_root, relative_path, business_format,
                   size_bytes, mtime_ns
            FROM files
            WHERE source_root=? AND current_presence_state='present'
              AND support_status='supported'
              AND business_format IN ('jpeg', 'png')
              AND sha256 IS NOT NULL
            ORDER BY relative_path
            """,
            [source_root],
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def text_candidates(self, source_root: str) -> list[dict[str, Any]]:
        """Return supported plain-text files for the deterministic text pass."""

        cursor = self.connection.execute(
            """
            SELECT file_id, sha256, source_root, relative_path, business_format,
                   size_bytes, mtime_ns
            FROM files
            WHERE source_root=? AND current_presence_state='present'
              AND support_status='supported'
              AND text_candidate=TRUE AND business_format='txt'
              AND sha256 IS NOT NULL
            ORDER BY relative_path
            """,
            [source_root],
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def recover_incomplete_extractions(self, source_root: str) -> int:
        now = utc_now()

        def recover() -> int:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM extraction_runs WHERE source_root=? AND status='running'",
                [source_root],
            ).fetchone()
            count = int(row[0] or 0)
            self.connection.execute(
                "UPDATE extraction_runs SET status='interrupted', finished_at=? WHERE source_root=? AND status='running'",
                [now, source_root],
            )
            return count

        return self._run_recovery_with_index_repair("extraction_runs", recover)

    def _reopen_after_fatal_write(self) -> None:
        """Reopen DuckDB after a driver-invalidating fatal write error."""

        try:
            self.connection.close()
        finally:
            self.connection = _connect_registry(self.path, read_only=False)

    def _repair_recovery_indexes(self, table: str) -> None:
        """Rebuild secondary indexes needed by stale-run recovery."""

        definitions = _RECOVERY_INDEX_STATEMENTS.get(table, ())
        if not definitions:
            return
        schema_lock = FileLock(self.path.with_name(self.path.name + ".schema.lock"))
        schema_lock.acquire(timeout=30.0)
        try:
            for index_name, _statement in definitions:
                self.connection.execute(f"DROP INDEX IF EXISTS {index_name}")
            for _index_name, statement in definitions:
                self.connection.execute(statement)
        finally:
            schema_lock.release()

    def _run_recovery_with_index_repair(self, table: str, operation) -> Any:
        """Run one recovery write, repairing a driver-invalidated index once."""

        try:
            return operation()
        except duckdb.FatalException:
            # DuckDB invalidates the connection after this class of fatal
            # index error.  Always reconnect before any repair or retry.
            self._reopen_after_fatal_write()
            self._repair_recovery_indexes(table)
            return operation()

    def _recover_incomplete_table(self, table: str, status_column: str, now: datetime) -> int:
        row = self.connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {status_column}='running'"
        ).fetchone()
        count = int(row[0] or 0)
        if count:
            self.connection.execute(
                f"UPDATE {table} SET status='interrupted', finished_at=? WHERE {status_column}='running'",
                [now],
            )
        return count

    def recover_all_incomplete_runs(self) -> dict[str, int]:
        """Recover runs left open by a previous dead server instance.

        This is called once after the new server owns the project lock and
        before it accepts processing requests. Existing artifacts are never
        removed or rewritten; only durable run-state markers are finalized.
        """

        now = utc_now()
        recovered: dict[str, int] = {}
        for table, status_column in (
            ("scan_runs", "status"),
            ("extraction_runs", "status"),
            ("cleaning_runs", "status"),
            ("semantic_runs", "status"),
        ):
            try:
                count = self._run_recovery_with_index_repair(
                    table,
                    lambda: self._recover_incomplete_table(table, status_column, now),
                )
                recovered[table] = count
            except duckdb.CatalogException:
                # Older registries may not have a later phase table. Their
                # existing recovery paths remain compatible with this sweep.
                recovered[table] = 0
        return recovered

    def reusable_extraction(self, extraction_identity: str) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """
            SELECT extraction_run_id, file_id, content_sha256, status, table_count, sheet_count,
                   quality_issue_count, total_rows, total_bytes, timings_json
            FROM extraction_runs
            WHERE extraction_identity=? AND status IN ('successful', 'partial')
            ORDER BY finished_at DESC LIMIT 1
            """,
            [extraction_identity],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        result = dict(zip(columns, row))
        paths_cursor = self.connection.execute(
            """
            SELECT raw_artifact_path, normalized_artifact_path, metadata_artifact_path
            FROM table_assets WHERE extraction_run_id=? AND is_current=TRUE
            """,
            [result["extraction_run_id"]],
        )
        result["artifacts"] = [item for row_paths in paths_cursor.fetchall() for item in row_paths if item]
        return result

    def reusable_pdf_extraction(self, extraction_identity: str) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """
            SELECT extraction_run_id, file_id, content_sha256, status, quality_issue_count,
                   total_bytes, timings_json, warnings_json
            FROM extraction_runs
            WHERE extraction_identity=? AND status IN ('successful', 'partial')
            ORDER BY finished_at DESC LIMIT 1
            """,
            [extraction_identity],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        result = dict(zip(columns, row))
        warnings = result.get("warnings_json")
        if isinstance(warnings, str):
            try:
                warnings = json.loads(warnings)
            except json.JSONDecodeError:
                warnings = {}
        if not isinstance(warnings, dict):
            warnings = {}
        result["profile"] = warnings.get("profile") or {}
        result["profile_artifact_path"] = warnings.get("profile_artifact_path")
        paths_cursor = self.connection.execute(
            """
            SELECT raw_artifact_path, normalized_artifact_path, metadata_artifact_path
            FROM text_assets WHERE extraction_run_id=? AND is_current=TRUE
            """,
            [result["extraction_run_id"]],
        )
        result["artifacts"] = [item for row_paths in paths_cursor.fetchall() for item in row_paths if item]
        if result["profile_artifact_path"]:
            result["artifacts"].append(result["profile_artifact_path"])
        return result

    def current_pdf_profile(self, file_id: str, content_sha256: str) -> dict[str, Any] | None:
        """Return the most recent Phase 4A profile for one content identity."""

        cursor = self.connection.execute(
            """
            SELECT extraction_run_id, status, warnings_json
            FROM extraction_runs
            WHERE file_id=? AND content_sha256=? AND attempted_route='pdf_native_text'
              AND status IN ('successful', 'partial')
            ORDER BY finished_at DESC NULLS LAST, started_at DESC
            LIMIT 1
            """,
            [file_id, content_sha256],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        warnings = row[2]
        if isinstance(warnings, str):
            try:
                warnings = json.loads(warnings)
            except json.JSONDecodeError:
                warnings = {}
        if not isinstance(warnings, dict):
            warnings = {}
        profile = warnings.get("profile")
        if not isinstance(profile, dict):
            return None
        return {
            "extraction_run_id": row[0],
            "status": row[1],
            "profile": profile,
        }

    def reusable_pdf_table_extraction(self, extraction_identity: str) -> dict[str, Any] | None:
        """Return a successful/partial/deferred candidate run with its artifacts."""

        cursor = self.connection.execute(
            """
            SELECT extraction_run_id, file_id, content_sha256, status,
                   table_count, quality_issue_count, total_rows, total_bytes,
                   timings_json, warnings_json
            FROM extraction_runs
            WHERE extraction_identity=? AND attempted_route='pdf_table_candidate'
              AND status IN ('successful', 'partial', 'deferred_to_ocr')
            ORDER BY finished_at DESC NULLS LAST, started_at DESC
            LIMIT 1
            """,
            [extraction_identity],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        result = dict(zip(columns, row))
        warnings = result.get("warnings_json")
        if isinstance(warnings, str):
            try:
                warnings = json.loads(warnings)
            except json.JSONDecodeError:
                warnings = {}
        if not isinstance(warnings, dict):
            warnings = {}
        result["warnings"] = warnings
        table_cursor = self.connection.execute(
            """
            SELECT table_id, page_number, row_count, column_count,
                   raw_artifact_path, normalized_artifact_path, metadata_artifact_path
            FROM table_assets
            WHERE extraction_run_id=? AND is_current=TRUE
            ORDER BY page_number NULLS LAST, table_id
            """,
            [result["extraction_run_id"]],
        )
        table_columns = [item[0] for item in table_cursor.description]
        tables = [dict(zip(table_columns, table_row)) for table_row in table_cursor.fetchall()]
        result["tables"] = tables
        result["artifacts"] = [
            artifact
            for table in tables
            for artifact in (
                table.get("raw_artifact_path"),
                table.get("normalized_artifact_path"),
                table.get("metadata_artifact_path"),
            )
            if artifact
        ]
        return result

    def reusable_ocr_extraction(self, extraction_identity: str) -> dict[str, Any] | None:
        """Return a reusable OCR result and all published text artifacts."""

        cursor = self.connection.execute(
            """
            SELECT extraction_run_id, file_id, content_sha256, status,
                   quality_issue_count, total_bytes, timings_json, warnings_json,
                   table_count
            FROM extraction_runs
            WHERE extraction_identity=? AND attempted_route='ocr_rapidocr'
              AND status IN ('successful', 'partial')
            ORDER BY finished_at DESC NULLS LAST, started_at DESC
            LIMIT 1
            """,
            [extraction_identity],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        result = dict(zip(columns, row))
        paths_cursor = self.connection.execute(
            """
            SELECT text_asset_id, raw_artifact_path, normalized_artifact_path,
                   metadata_artifact_path
            FROM text_assets
            WHERE extraction_run_id=? AND is_current=TRUE
            ORDER BY page_number NULLS LAST, text_asset_id
            """,
            [result["extraction_run_id"]],
        )
        result["text_assets"] = [
            {
                "text_asset_id": item[0],
                "raw_artifact_path": item[1],
                "normalized_artifact_path": item[2],
                "metadata_artifact_path": item[3],
            }
            for item in paths_cursor.fetchall()
        ]
        result["text_asset_count"] = len(result["text_assets"])
        table_cursor = self.connection.execute(
            """
            SELECT table_id, raw_artifact_path, normalized_artifact_path,
                   metadata_artifact_path
            FROM table_assets
            WHERE extraction_run_id=? AND is_current=TRUE
            ORDER BY page_number NULLS LAST, table_id
            """,
            [result["extraction_run_id"]],
        )
        result["table_assets"] = [
            {
                "table_id": item[0],
                "raw_artifact_path": item[1],
                "normalized_artifact_path": item[2],
                "metadata_artifact_path": item[3],
            }
            for item in table_cursor.fetchall()
        ]
        result["artifacts"] = [
            artifact
            for asset in result["text_assets"]
            for artifact in (
                asset.get("raw_artifact_path"),
                asset.get("normalized_artifact_path"),
                asset.get("metadata_artifact_path"),
            )
            if artifact
        ]
        result["artifacts"].extend(
            artifact
            for asset in result["table_assets"]
            for artifact in (
                asset.get("raw_artifact_path"),
                asset.get("normalized_artifact_path"),
                asset.get("metadata_artifact_path"),
            )
            if artifact
        )
        return result

    def reusable_vision_extraction(self, extraction_identity: str) -> dict[str, Any] | None:
        """Return a reusable explicit Vision result and its asset artifacts."""

        cursor = self.connection.execute(
            """
            SELECT extraction_run_id, file_id, content_sha256, status,
                   quality_issue_count, total_bytes, timings_json, warnings_json,
                   table_count, total_rows
            FROM extraction_runs
            WHERE extraction_identity=? AND attempted_route='vision_llm'
              AND status IN ('successful', 'partial')
            ORDER BY finished_at DESC NULLS LAST, started_at DESC
            LIMIT 1
            """,
            [extraction_identity],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        result = dict(zip(columns, row))
        text_cursor = self.connection.execute(
            """
            SELECT text_asset_id, raw_artifact_path, normalized_artifact_path,
                   metadata_artifact_path
            FROM text_assets
            WHERE extraction_run_id=? AND is_current=TRUE
            ORDER BY text_asset_id
            """,
            [result["extraction_run_id"]],
        )
        result["text_assets"] = [
            {
                "text_asset_id": item[0],
                "raw_artifact_path": item[1],
                "normalized_artifact_path": item[2],
                "metadata_artifact_path": item[3],
            }
            for item in text_cursor.fetchall()
        ]
        table_cursor = self.connection.execute(
            """
            SELECT table_id, raw_artifact_path, normalized_artifact_path,
                   metadata_artifact_path
            FROM table_assets
            WHERE extraction_run_id=? AND is_current=TRUE
            ORDER BY table_id
            """,
            [result["extraction_run_id"]],
        )
        result["table_assets"] = [
            {
                "table_id": item[0],
                "raw_artifact_path": item[1],
                "normalized_artifact_path": item[2],
                "metadata_artifact_path": item[3],
            }
            for item in table_cursor.fetchall()
        ]
        result["artifacts"] = [
            artifact
            for asset in (*result["text_assets"], *result["table_assets"])
            for artifact in (
                asset.get("raw_artifact_path"),
                asset.get("normalized_artifact_path"),
                asset.get("metadata_artifact_path"),
            )
            if artifact
        ]
        return result

    def reusable_text_extraction(self, extraction_identity: str) -> dict[str, Any] | None:
        """Return one reusable deterministic TXT extraction and its artifacts."""

        cursor = self.connection.execute(
            """
            SELECT extraction_run_id, file_id, content_sha256, status,
                   quality_issue_count, total_bytes, timings_json, warnings_json,
                   table_count, total_rows
            FROM extraction_runs
            WHERE extraction_identity=? AND attempted_route='text_plain'
              AND status IN ('successful', 'partial')
            ORDER BY finished_at DESC NULLS LAST, started_at DESC
            LIMIT 1
            """,
            [extraction_identity],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        result = dict(zip(columns, row))
        asset_cursor = self.connection.execute(
            """
            SELECT text_asset_id, raw_artifact_path, normalized_artifact_path,
                   metadata_artifact_path
            FROM text_assets
            WHERE extraction_run_id=? AND is_current=TRUE
            """,
            [result["extraction_run_id"]],
        )
        result["text_assets"] = [
            {
                "text_asset_id": item[0],
                "raw_artifact_path": item[1],
                "normalized_artifact_path": item[2],
                "metadata_artifact_path": item[3],
            }
            for item in asset_cursor.fetchall()
        ]
        result["text_asset_count"] = len(result["text_assets"])
        result["text_chunk_count"] = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM text_chunks WHERE text_asset_id IN (SELECT text_asset_id FROM text_assets WHERE extraction_run_id=?)",
                [result["extraction_run_id"]],
            ).fetchone()[0]
            or 0
        )
        result["artifacts"] = [
            artifact
            for asset in result["text_assets"]
            for artifact in (
                asset.get("raw_artifact_path"),
                asset.get("normalized_artifact_path"),
                asset.get("metadata_artifact_path"),
            )
            if artifact
        ]
        return result

    def recover_incomplete_cleaning(self, source_root: str) -> int:
        now = utc_now()

        def recover() -> int:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM cleaning_runs WHERE source_root=? AND status='running'",
                [source_root],
            ).fetchone()
            count = int(row[0] or 0)
            self.connection.execute(
                "UPDATE cleaning_runs SET status='interrupted', finished_at=? WHERE source_root=? AND status='running'",
                [now, source_root],
            )
            return count

        return self._run_recovery_with_index_repair("cleaning_runs", recover)

    def cleaning_candidates(self, source_root: str) -> list[dict[str, Any]]:
        """Return current extracted assets without exposing source files to writers."""

        table_cursor = self.connection.execute(
            """
            SELECT t.table_id AS asset_id, 'table' AS asset_type, t.file_id,
                   t.content_sha256, t.extraction_run_id, t.extractor,
                   t.extractor_version, t.source_kind, t.source_relative_path,
                   t.sheet_name, t.page_number, t.bbox_json, t.row_count,
                   t.column_count, t.columns_json, t.raw_artifact_path,
                   t.normalized_artifact_path AS extraction_normalized_artifact_path,
                   t.metadata_artifact_path, t.extraction_confidence,
                   f.source_root, f.filename, f.business_format
            FROM table_assets t
            JOIN files f ON f.file_id=t.file_id
            WHERE t.is_current=TRUE AND f.source_root=? AND f.current_presence_state='present'
            ORDER BY t.source_relative_path, t.page_number NULLS FIRST, t.table_id
            """,
            [source_root],
        )
        table_columns = [item[0] for item in table_cursor.description]
        rows = [dict(zip(table_columns, row)) for row in table_cursor.fetchall()]

        text_cursor = self.connection.execute(
            """
            SELECT t.text_asset_id AS asset_id, 'text' AS asset_type, t.file_id,
                   t.content_sha256, t.extraction_run_id, t.extractor,
                   t.extractor_version, t.source_kind, t.source_relative_path,
                   t.section AS sheet_name, t.page_number, t.bbox_json,
                   t.text, t.language, t.raw_artifact_path,
                   t.normalized_artifact_path AS extraction_normalized_artifact_path,
                   t.metadata_artifact_path, CAST(NULL AS DOUBLE) AS extraction_confidence,
                   COALESCE((SELECT COUNT(*) FROM text_chunks tc WHERE tc.text_asset_id=t.text_asset_id), 0) AS chunk_count,
                   f.source_root, f.filename, f.business_format
            FROM text_assets t
            JOIN files f ON f.file_id=t.file_id
            WHERE t.is_current=TRUE AND f.source_root=? AND f.current_presence_state='present'
            ORDER BY t.source_relative_path, t.page_number NULLS FIRST, t.text_asset_id
            """,
            [source_root],
        )
        text_columns = [item[0] for item in text_cursor.description]
        rows.extend(dict(zip(text_columns, row)) for row in text_cursor.fetchall())
        return rows

    def reusable_cleaning(self, cleaning_identity: str) -> dict[str, Any] | None:
        cursor = self.connection.execute(
            """
            SELECT cleaning_run_id, cleaning_identity, asset_id, asset_type, file_id,
                   content_sha256, raw_artifact_identity, cleaner, cleaner_version,
                   config_version, source_root, source_relative_path, started_at,
                   finished_at, status, force, normalized_artifact_path,
                   manifest_artifact_path, profile_artifact_path, profile_json,
                   timings_json, quality_status, warnings_json, error_category,
                   error_message
            FROM cleaning_runs
            WHERE cleaning_identity=? AND status='successful'
            ORDER BY finished_at DESC NULLS LAST, started_at DESC
            LIMIT 1
            """,
            [cleaning_identity],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        return dict(zip(columns, row))

    def start_cleaning_run(self, result: Any, *, started_at: datetime, force: bool) -> None:
        self.connection.execute(
            """
            INSERT INTO cleaning_runs(
                cleaning_run_id, cleaning_identity, asset_id, asset_type, file_id,
                content_sha256, raw_artifact_identity, cleaner, cleaner_version,
                config_version, source_root, source_relative_path, started_at,
                status, force, quality_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, 'needs_review')
            """,
            [
                result.cleaning_run_id,
                result.cleaning_identity,
                result.asset_id,
                result.asset_type,
                result.file_id,
                result.content_sha256,
                result.raw_artifact_identity,
                result.cleaner,
                result.cleaner_version,
                result.config_version,
                result.source_root,
                result.source_relative_path,
                started_at,
                force,
            ],
        )

    def record_cleaning_result(
        self,
        result: Any,
        *,
        started_at: datetime,
        finished_at: datetime,
        force: bool,
    ) -> None:
        """Persist one clean/profile result while retaining every raw artifact."""

        connection = self.connection
        profile = result.profile or {}
        timings = result.timings.as_dict() if hasattr(result.timings, "as_dict") else dict(result.timings or {})
        warnings = list(result.warnings or [])
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO cleaning_runs(
                    cleaning_run_id, cleaning_identity, asset_id, asset_type, file_id,
                    content_sha256, raw_artifact_identity, cleaner, cleaner_version,
                    config_version, source_root, source_relative_path, started_at,
                    finished_at, status, force, normalized_artifact_path,
                    manifest_artifact_path, profile_artifact_path, profile_json,
                    timings_json, quality_status, warnings_json, error_category,
                    error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    result.cleaning_run_id,
                    result.cleaning_identity,
                    result.asset_id,
                    result.asset_type,
                    result.file_id,
                    result.content_sha256,
                    result.raw_artifact_identity,
                    result.cleaner,
                    result.cleaner_version,
                    result.config_version,
                    result.source_root,
                    result.source_relative_path,
                    started_at,
                    finished_at,
                    result.status,
                    force,
                    result.normalized_artifact_path,
                    result.manifest_artifact_path,
                    result.profile_artifact_path,
                    json.dumps(profile, ensure_ascii=False, default=str),
                    json.dumps(timings, ensure_ascii=False, default=str),
                    result.quality_status,
                    json.dumps(warnings, ensure_ascii=False, default=str),
                    result.error_category,
                    result.error_message,
                ],
            )
            if result.status == "successful":
                if result.asset_type == "table":
                    row = result.profile_row
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO table_profiles(
                            profile_id, table_id, cleaning_run_id, cleaning_identity,
                            content_sha256, row_count, column_count, null_count,
                            null_ratio, empty_row_count_before, empty_column_count_before,
                            exact_duplicate_row_count, empty_cell_ratio, long_text_cell_ratio,
                            irregular_row_width, ocr_mean_confidence, ocr_min_confidence,
                            provenance_complete, profile_json, profile_artifact_path,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            row["profile_id"], row["table_id"], result.cleaning_run_id,
                            result.cleaning_identity, result.content_sha256,
                            row["row_count"], row["column_count"], row["null_count"],
                            row["null_ratio"], row["empty_row_count_before"],
                            row["empty_column_count_before"], row["exact_duplicate_row_count"],
                            row["empty_cell_ratio"], row["long_text_cell_ratio"],
                            row["irregular_row_width"], row.get("ocr_mean_confidence"),
                            row.get("ocr_min_confidence"), row["provenance_complete"],
                            json.dumps(profile, ensure_ascii=False, default=str),
                            result.profile_artifact_path, finished_at,
                        ],
                    )
                else:
                    row = result.profile_row
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO text_profiles(
                            profile_id, text_asset_id, cleaning_run_id, cleaning_identity,
                            content_sha256, char_count, line_count, page_count, block_count,
                            chunk_count, language_hint, extraction_source,
                            ocr_mean_confidence, empty_content, low_content, profile_json,
                            profile_artifact_path, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            row["profile_id"], row["text_asset_id"], result.cleaning_run_id,
                            result.cleaning_identity, result.content_sha256,
                            row["char_count"], row["line_count"], row["page_count"],
                            row["block_count"], row["chunk_count"], row.get("language_hint"),
                            row["extraction_source"], row.get("ocr_mean_confidence"),
                            row["empty_content"], row["low_content"],
                            json.dumps(profile, ensure_ascii=False, default=str),
                            result.profile_artifact_path, finished_at,
                        ],
                    )
            for issue in result.issues or ():
                connection.execute(
                    """
                    INSERT INTO quality_issues(
                        issue_id, extraction_run_id, cleaning_run_id, asset_id, severity,
                        issue_type, description, evidence_json, detected_by,
                        suggested_action, status, created_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                        cleaning_run_id=excluded.cleaning_run_id,
                        evidence_json=excluded.evidence_json,
                        description=excluded.description,
                        status=excluded.status
                    """,
                    [
                        issue.issue_id,
                        result.cleaning_run_id,
                        issue.asset_id,
                        issue.severity.value,
                        issue.issue_type,
                        issue.description,
                        json.dumps(issue.evidence, ensure_ascii=False, default=str),
                        issue.detected_by,
                        issue.suggested_action,
                        issue.status.value,
                        finished_at,
                    ],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def start_structured_extraction(
        self,
        *,
        extraction_run_id: str,
        extraction_identity: str,
        source: Any,
        extractor: str,
        extractor_version: str,
        started_at: datetime,
        force: bool,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO extraction_runs(
                extraction_run_id, file_id, content_sha256, extraction_identity,
                source_root, source_relative_path, started_at, status, force,
                pipeline_version, configuration_version, attempted_route,
                route_reason, extractor_versions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            [
                extraction_run_id,
                source.file_id,
                source.content_sha256,
                extraction_identity,
                source.source_root,
                source.relative_path,
                started_at,
                force,
                paths.PIPELINE_VERSION,
                paths.STRUCTURED_CONFIG_VERSION,
                "structured_native",
                f"supported_{source.business_format}_table",
                json.dumps({extractor: extractor_version}),
            ],
        )

    def start_pdf_extraction(
        self,
        *,
        extraction_run_id: str,
        extraction_identity: str,
        source: Any,
        extractor: str,
        extractor_version: str,
        started_at: datetime,
        force: bool,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO extraction_runs(
                extraction_run_id, file_id, content_sha256, extraction_identity,
                source_root, source_relative_path, started_at, status, force,
                pipeline_version, configuration_version, attempted_route,
                route_reason, extractor_versions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            [
                extraction_run_id,
                source.file_id,
                source.content_sha256,
                extraction_identity,
                source.source_root,
                source.relative_path,
                started_at,
                force,
                paths.PIPELINE_VERSION,
                paths.PDF_CONFIG_VERSION,
                "pdf_native_text",
                "supported_pdf_text",
                json.dumps({extractor: extractor_version}),
            ],
        )

    def start_pdf_table_extraction(
        self,
        *,
        extraction_run_id: str,
        extraction_identity: str,
        source: Any,
        extractor: str,
        extractor_version: str,
        started_at: datetime,
        force: bool,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO extraction_runs(
                extraction_run_id, file_id, content_sha256, extraction_identity,
                source_root, source_relative_path, started_at, status, force,
                pipeline_version, configuration_version, attempted_route,
                route_reason, extractor_versions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            [
                extraction_run_id,
                source.file_id,
                source.content_sha256,
                extraction_identity,
                source.source_root,
                source.relative_path,
                started_at,
                force,
                paths.PDF_TABLE_PIPELINE_VERSION,
                paths.PDF_TABLE_CONFIG_VERSION,
                "pdf_table_candidate",
                "img2table_candidate_native_text_only",
                json.dumps({extractor: extractor_version}),
            ],
        )

    def start_ocr_extraction(
        self,
        *,
        extraction_run_id: str,
        extraction_identity: str,
        source: Any,
        extractor: str,
        extractor_version: str,
        started_at: datetime,
        force: bool,
        route_reason: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO extraction_runs(
                extraction_run_id, file_id, content_sha256, extraction_identity,
                source_root, source_relative_path, started_at, status, force,
                pipeline_version, configuration_version, attempted_route,
                route_reason, extractor_versions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            [
                extraction_run_id,
                source.file_id,
                source.content_sha256,
                extraction_identity,
                source.source_root,
                source.relative_path,
                started_at,
                force,
                paths.OCR_PIPELINE_VERSION,
                paths.OCR_CONFIG_VERSION,
                "ocr_rapidocr",
                route_reason,
                json.dumps({extractor: extractor_version}),
            ],
        )

    def start_vision_extraction(
        self,
        *,
        extraction_run_id: str,
        extraction_identity: str,
        source: Any,
        extractor: str,
        extractor_version: str,
        started_at: datetime,
        force: bool,
        route_reason: str,
        provider_contract: str,
        provider_model: str,
    ) -> None:
        """Persist the in-progress marker for an explicit image Vision run."""

        self.connection.execute(
            """
            INSERT INTO extraction_runs(
                extraction_run_id, file_id, content_sha256, extraction_identity,
                source_root, source_relative_path, started_at, status, force,
                pipeline_version, configuration_version, attempted_route,
                route_reason, extractor_versions_json, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                extraction_run_id,
                source.file_id,
                source.content_sha256,
                extraction_identity,
                source.source_root,
                source.relative_path,
                started_at,
                force,
                paths.VISION_PIPELINE_VERSION,
                paths.VISION_CONFIG_VERSION,
                "vision_llm",
                route_reason,
                json.dumps(
                    {
                        extractor: extractor_version,
                        "provider_contract": provider_contract,
                        "model": provider_model,
                    },
                    ensure_ascii=False,
                ),
                json.dumps({"provider_contract": provider_contract, "model": provider_model}, ensure_ascii=False),
            ],
        )

    def start_text_extraction(
        self,
        *,
        extraction_run_id: str,
        extraction_identity: str,
        source: Any,
        extractor: str,
        extractor_version: str,
        started_at: datetime,
        force: bool,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO extraction_runs(
                extraction_run_id, file_id, content_sha256, extraction_identity,
                source_root, source_relative_path, started_at, status, force,
                pipeline_version, configuration_version, attempted_route,
                route_reason, extractor_versions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            [
                extraction_run_id,
                source.file_id,
                source.content_sha256,
                extraction_identity,
                source.source_root,
                source.relative_path,
                started_at,
                force,
                paths.TEXT_PIPELINE_VERSION,
                paths.TEXT_CONFIG_VERSION,
                "text_plain",
                "supported_txt_text",
                json.dumps({extractor: extractor_version}),
            ],
        )

    def record_structured_result(
        self,
        result: Any,
        *,
        started_at: datetime,
        finished_at: datetime,
        force: bool,
    ) -> None:
        """Persist one isolated file result; callers remain the single writer."""

        connection = self.connection
        connection.execute("BEGIN TRANSACTION")
        try:
            if result.status in {"successful", "partial"}:
                connection.execute(
                    """
                    UPDATE table_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor],
                )
            elif result.status == "failed":
                # Preserve a prior same-content result during a transient
                # failure, but never present an old-content asset as current
                # after the source itself changed.
                connection.execute(
                    """
                    UPDATE table_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND content_sha256<>? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor, result.source.content_sha256],
                )
            connection.execute(
                """
                INSERT OR REPLACE INTO extraction_runs(
                    extraction_run_id, file_id, content_sha256, extraction_identity,
                    source_root, source_relative_path, started_at, finished_at, status,
                    force, pipeline_version, configuration_version, attempted_route,
                    route_reason, timings_json, warnings_json, extractor_versions_json,
                    table_count, sheet_count, quality_issue_count, total_rows, total_bytes,
                    error_category, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    result.extraction_run_id,
                    result.source.file_id,
                    result.source.content_sha256,
                    result.extraction_identity,
                    result.source.source_root,
                    result.source.relative_path,
                    started_at,
                    finished_at,
                    result.status,
                    force,
                    paths.PIPELINE_VERSION,
                    paths.STRUCTURED_CONFIG_VERSION,
                    "structured_native",
                    f"supported_{result.source.business_format}_table",
                    json.dumps(result.timings.as_dict()),
                    json.dumps(result.warnings, ensure_ascii=False, default=str),
                    json.dumps({result.extractor: result.extractor_version}),
                    len(result.assets),
                    result.sheet_count,
                    len(result.issues),
                    result.total_rows,
                    result.source.size_bytes,
                    result.error_category,
                    result.error_message,
                ],
            )
            for asset in result.assets:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO table_assets(
                        table_id, file_id, content_sha256, extraction_run_id, extractor,
                        extractor_version, source_kind, source_relative_path, sheet_name,
                        page_number, bbox_json, source_row_start, source_row_end,
                        source_column_start, source_column_end, row_count, column_count,
                        columns_json, raw_artifact_path, normalized_artifact_path,
                        metadata_artifact_path, extraction_confidence, quality_status,
                        created_at, is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
                    """,
                    [
                        asset.table_id,
                        asset.file_id,
                        asset.content_sha256,
                        asset.extraction_run_id,
                        asset.extractor,
                        asset.extractor_version,
                        asset.source_kind.value,
                        asset.source_relative_path,
                        asset.sheet_name,
                        asset.page_number,
                        json.dumps(asset.bbox.__dict__) if asset.bbox else None,
                        asset.source_row_start,
                        asset.source_row_end,
                        asset.source_column_start,
                        asset.source_column_end,
                        asset.row_count,
                        asset.column_count,
                        json.dumps(asset.columns, ensure_ascii=False),
                        asset.raw_artifact_path,
                        asset.normalized_artifact_path,
                        asset.metadata_artifact_path,
                        asset.extraction_confidence,
                        asset.quality_status.value,
                        asset.created_at.replace(tzinfo=None),
                    ],
                )
            for issue in result.issues:
                connection.execute(
                    """
                    INSERT INTO quality_issues(
                        issue_id, extraction_run_id, asset_id, severity, issue_type,
                        description, evidence_json, detected_by, suggested_action,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                        extraction_run_id=excluded.extraction_run_id,
                        evidence_json=excluded.evidence_json,
                        description=excluded.description
                    """,
                    [
                        issue.issue_id,
                        result.extraction_run_id,
                        issue.asset_id,
                        issue.severity.value,
                        issue.issue_type,
                        issue.description,
                        json.dumps(issue.evidence, ensure_ascii=False, default=str),
                        issue.detected_by,
                        issue.suggested_action,
                        issue.status.value,
                        finished_at,
                    ],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def record_pdf_result(
        self,
        result: Any,
        *,
        started_at: datetime,
        finished_at: datetime,
        force: bool,
    ) -> None:
        """Persist one isolated PDF text result through the single writer."""

        connection = self.connection
        connection.execute("BEGIN TRANSACTION")
        try:
            if result.status in {"successful", "partial"}:
                connection.execute(
                    """
                    UPDATE text_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor],
                )
            elif result.status == "failed":
                connection.execute(
                    """
                    UPDATE text_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND content_sha256<>? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor, result.source.content_sha256],
                )
            profile = result.profile.as_dict() if result.profile is not None else {}
            warnings_payload = {
                "warnings": result.warnings,
                "profile": profile,
                "profile_artifact_path": result.profile_artifact_path,
            }
            connection.execute(
                """
                INSERT OR REPLACE INTO extraction_runs(
                    extraction_run_id, file_id, content_sha256, extraction_identity,
                    source_root, source_relative_path, started_at, finished_at, status,
                    force, pipeline_version, configuration_version, attempted_route,
                    route_reason, timings_json, warnings_json, extractor_versions_json,
                    table_count, sheet_count, quality_issue_count, total_rows, total_bytes,
                    error_category, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    result.extraction_run_id,
                    result.source.file_id,
                    result.source.content_sha256,
                    result.extraction_identity,
                    result.source.source_root,
                    result.source.relative_path,
                    started_at,
                    finished_at,
                    result.status,
                    force,
                    paths.PIPELINE_VERSION,
                    paths.PDF_CONFIG_VERSION,
                    "pdf_native_text",
                    "supported_pdf_text",
                    json.dumps(result.timings.as_dict()),
                    json.dumps(warnings_payload, ensure_ascii=False, default=str),
                    json.dumps({result.extractor: result.extractor_version}),
                    0,
                    0,
                    len(result.issues),
                    0,
                    result.source.size_bytes,
                    result.error_category,
                    result.error_message,
                ],
            )
            for asset in result.text_assets:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO text_assets(
                        text_asset_id, file_id, content_sha256, extraction_run_id,
                        extractor, extractor_version, source_kind, page_number, section,
                        bbox_json, text, language, created_at, source_relative_path,
                        raw_artifact_path, normalized_artifact_path, metadata_artifact_path,
                        is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
                    """,
                    [
                        asset.text_asset_id,
                        asset.file_id,
                        asset.content_sha256,
                        asset.extraction_run_id,
                        asset.extractor,
                        asset.extractor_version,
                        asset.source_kind.value,
                        asset.page_number,
                        asset.section,
                        json.dumps(asset.bbox.__dict__) if asset.bbox else None,
                        asset.text,
                        asset.language,
                        asset.created_at.replace(tzinfo=None),
                        asset.source_relative_path,
                        asset.raw_artifact_path,
                        asset.normalized_artifact_path,
                        asset.metadata_artifact_path,
                    ],
                )
            for chunk in result.text_chunks:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO text_chunks(
                        chunk_id, text_asset_id, file_id, chunk_index, text,
                        char_start, char_end, provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        chunk.chunk_id,
                        chunk.text_asset_id,
                        chunk.file_id,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.char_start,
                        chunk.char_end,
                        json.dumps(chunk.provenance.__dict__, ensure_ascii=False, default=str),
                    ],
                )
            for issue in result.issues:
                connection.execute(
                    """
                    INSERT INTO quality_issues(
                        issue_id, extraction_run_id, asset_id, severity, issue_type,
                        description, evidence_json, detected_by, suggested_action,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                        extraction_run_id=excluded.extraction_run_id,
                        evidence_json=excluded.evidence_json,
                        description=excluded.description
                    """,
                    [
                        issue.issue_id,
                        result.extraction_run_id,
                        issue.asset_id,
                        issue.severity.value,
                        issue.issue_type,
                        issue.description,
                        json.dumps(issue.evidence, ensure_ascii=False, default=str),
                        issue.detected_by,
                        issue.suggested_action,
                        issue.status.value,
                        finished_at,
                    ],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def record_ocr_result(
        self,
        result: Any,
        *,
        started_at: datetime,
        finished_at: datetime,
        force: bool,
    ) -> None:
        """Persist one independent RapidOCR result through the single writer.

        OCR text is intentionally stored under its own extractor and route so
        it can coexist with native PDF text.  A later OCR rerun never replaces
        the native ``pymupdf-native-text`` assets.
        """

        connection = self.connection
        connection.execute("BEGIN TRANSACTION")
        try:
            if result.status in {"successful", "partial"}:
                connection.execute(
                    """
                    UPDATE text_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor],
                )
            elif result.status == "failed":
                connection.execute(
                    """
                    UPDATE text_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND content_sha256<>? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor, result.source.content_sha256],
                )
            # The integrated route owns the image-table extractor even when a
            # page/image yields zero candidates, so a forced rerun must retire
            # an older same-route table set rather than leave stale assets.
            table_extractors = {"img2table-image"}
            table_extractors.update(asset.extractor for asset in result.table_assets)
            for table_extractor in table_extractors:
                owns_new_table_set = result.status == "successful" or (
                    result.status == "partial" and bool(result.table_assets)
                )
                if owns_new_table_set:
                    connection.execute(
                        """
                        UPDATE table_assets SET is_current=FALSE
                        WHERE file_id=? AND extractor=? AND is_current=TRUE
                        """,
                        [result.source.file_id, table_extractor],
                    )
                elif result.status == "partial":
                    # Keep a prior same-content candidate visible when OCR
                    # text succeeded but the image table adapter failed
                    # before publishing any replacement table. Changed
                    # content is still retired so stale assets cannot look
                    # current for the new source SHA.
                    connection.execute(
                        """
                        UPDATE table_assets SET is_current=FALSE
                        WHERE file_id=? AND extractor=? AND content_sha256<>? AND is_current=TRUE
                        """,
                        [result.source.file_id, table_extractor, result.source.content_sha256],
                    )
                elif result.status == "failed":
                    connection.execute(
                        """
                        UPDATE table_assets SET is_current=FALSE
                        WHERE file_id=? AND extractor=? AND content_sha256<>? AND is_current=TRUE
                        """,
                        [result.source.file_id, table_extractor, result.source.content_sha256],
                    )
            warnings_payload = {
                "warnings": result.warnings,
                "ocr_targets": result.ocr_targets,
                "image_table_extractor": "img2table-image",
                "image_table_extractor_version": f"{paths.IMG2TABLE_VERSION}+ocrdata-v1",
                "ocr_reused_by_image_table": True,
            }
            connection.execute(
                """
                INSERT OR REPLACE INTO extraction_runs(
                    extraction_run_id, file_id, content_sha256, extraction_identity,
                    source_root, source_relative_path, started_at, finished_at, status,
                    force, pipeline_version, configuration_version, attempted_route,
                    route_reason, timings_json, warnings_json, extractor_versions_json,
                    table_count, sheet_count, quality_issue_count, total_rows, total_bytes,
                    error_category, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    result.extraction_run_id,
                    result.source.file_id,
                    result.source.content_sha256,
                    result.extraction_identity,
                    result.source.source_root,
                    result.source.relative_path,
                    started_at,
                    finished_at,
                    result.status,
                    force,
                    paths.OCR_PIPELINE_VERSION,
                    paths.OCR_CONFIG_VERSION,
                    "ocr_rapidocr",
                    result.route_reason,
                    json.dumps(result.timings.as_dict(), ensure_ascii=False, default=str),
                    json.dumps(warnings_payload, ensure_ascii=False, default=str),
                    json.dumps(
                        {
                            result.extractor: result.extractor_version,
                            "img2table-image": f"{paths.IMG2TABLE_VERSION}+ocrdata-v1",
                        }
                    ),
                    len(result.table_assets),
                    result.target_count,
                    len(result.issues),
                    result.total_rows,
                    result.source.size_bytes,
                    result.error_category,
                    result.error_message,
                ],
            )
            for asset in result.table_assets:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO table_assets(
                        table_id, file_id, content_sha256, extraction_run_id, extractor,
                        extractor_version, source_kind, source_relative_path, sheet_name,
                        page_number, bbox_json, source_row_start, source_row_end,
                        source_column_start, source_column_end, row_count, column_count,
                        columns_json, raw_artifact_path, normalized_artifact_path,
                        metadata_artifact_path, extraction_confidence, quality_status,
                        created_at, is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
                    """,
                    [
                        asset.table_id,
                        asset.file_id,
                        asset.content_sha256,
                        asset.extraction_run_id,
                        asset.extractor,
                        asset.extractor_version,
                        asset.source_kind.value,
                        asset.source_relative_path,
                        asset.sheet_name,
                        asset.page_number,
                        json.dumps(asset.bbox.__dict__) if asset.bbox else None,
                        asset.source_row_start,
                        asset.source_row_end,
                        asset.source_column_start,
                        asset.source_column_end,
                        asset.row_count,
                        asset.column_count,
                        json.dumps(asset.columns, ensure_ascii=False),
                        asset.raw_artifact_path,
                        asset.normalized_artifact_path,
                        asset.metadata_artifact_path,
                        asset.extraction_confidence,
                        asset.quality_status.value,
                        asset.created_at.replace(tzinfo=None),
                    ],
                )
            for asset in result.text_assets:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO text_assets(
                        text_asset_id, file_id, content_sha256, extraction_run_id,
                        extractor, extractor_version, source_kind, page_number, section,
                        bbox_json, text, language, created_at, source_relative_path,
                        raw_artifact_path, normalized_artifact_path, metadata_artifact_path,
                        is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
                    """,
                    [
                        asset.text_asset_id,
                        asset.file_id,
                        asset.content_sha256,
                        asset.extraction_run_id,
                        asset.extractor,
                        asset.extractor_version,
                        asset.source_kind.value,
                        asset.page_number,
                        asset.section,
                        json.dumps(asset.bbox.__dict__) if asset.bbox else None,
                        asset.text,
                        asset.language,
                        asset.created_at.replace(tzinfo=None),
                        asset.source_relative_path,
                        asset.raw_artifact_path,
                        asset.normalized_artifact_path,
                        asset.metadata_artifact_path,
                    ],
                )
            for chunk in result.text_chunks:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO text_chunks(
                        chunk_id, text_asset_id, file_id, chunk_index, text,
                        char_start, char_end, provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        chunk.chunk_id,
                        chunk.text_asset_id,
                        chunk.file_id,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.char_start,
                        chunk.char_end,
                        json.dumps(chunk.provenance.__dict__, ensure_ascii=False, default=str),
                    ],
                )
            for issue in result.issues:
                connection.execute(
                    """
                    INSERT INTO quality_issues(
                        issue_id, extraction_run_id, asset_id, severity, issue_type,
                        description, evidence_json, detected_by, suggested_action,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                        extraction_run_id=excluded.extraction_run_id,
                        evidence_json=excluded.evidence_json,
                        description=excluded.description
                    """,
                    [
                        issue.issue_id,
                        result.extraction_run_id,
                        issue.asset_id,
                        issue.severity.value,
                        issue.issue_type,
                        issue.description,
                        json.dumps(issue.evidence, ensure_ascii=False, default=str),
                        issue.detected_by,
                        issue.suggested_action,
                        issue.status.value,
                        finished_at,
                    ],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def record_vision_result(
        self,
        result: Any,
        *,
        started_at: datetime,
        finished_at: datetime,
        force: bool,
    ) -> None:
        """Persist one isolated Vision image result using existing assets."""

        connection = self.connection
        connection.execute("BEGIN TRANSACTION")
        try:
            for table_or_text, extractor in (("table_assets", "vision_llm"), ("text_assets", "vision_llm")):
                if result.status in {"successful", "partial"}:
                    connection.execute(
                        f"UPDATE {table_or_text} SET is_current=FALSE WHERE file_id=? AND extractor=? AND is_current=TRUE",
                        [result.source.file_id, extractor],
                    )
                elif result.status == "failed":
                    connection.execute(
                        f"""UPDATE {table_or_text} SET is_current=FALSE
                            WHERE file_id=? AND extractor=? AND content_sha256<>? AND is_current=TRUE""",
                        [result.source.file_id, extractor, result.source.content_sha256],
                    )
            warnings_payload = {
                "warnings": result.warnings,
                "provider_contract": paths.VISION_CONTRACT_VERSION,
                "route_reason": result.route_reason,
            }
            connection.execute(
                """
                INSERT OR REPLACE INTO extraction_runs(
                    extraction_run_id, file_id, content_sha256, extraction_identity,
                    source_root, source_relative_path, started_at, finished_at, status,
                    force, pipeline_version, configuration_version, attempted_route,
                    route_reason, timings_json, warnings_json, extractor_versions_json,
                    table_count, sheet_count, quality_issue_count, total_rows, total_bytes,
                    error_category, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    result.extraction_run_id,
                    result.source.file_id,
                    result.source.content_sha256,
                    result.extraction_identity,
                    result.source.source_root,
                    result.source.relative_path,
                    started_at,
                    finished_at,
                    result.status,
                    force,
                    paths.VISION_PIPELINE_VERSION,
                    paths.VISION_CONFIG_VERSION,
                    "vision_llm",
                    result.route_reason,
                    json.dumps(result.timings.as_dict(), ensure_ascii=False, default=str),
                    json.dumps(warnings_payload, ensure_ascii=False, default=str),
                    json.dumps({result.extractor: result.extractor_version}, ensure_ascii=False),
                    len(result.table_assets),
                    0,
                    len(result.issues),
                    result.total_rows,
                    result.source.size_bytes,
                    result.error_category,
                    result.error_message,
                ],
            )
            for asset in result.table_assets:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO table_assets(
                        table_id, file_id, content_sha256, extraction_run_id, extractor,
                        extractor_version, source_kind, source_relative_path, sheet_name,
                        page_number, bbox_json, source_row_start, source_row_end,
                        source_column_start, source_column_end, row_count, column_count,
                        columns_json, raw_artifact_path, normalized_artifact_path,
                        metadata_artifact_path, extraction_confidence, quality_status,
                        created_at, is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
                    """,
                    [
                        asset.table_id,
                        asset.file_id,
                        asset.content_sha256,
                        asset.extraction_run_id,
                        asset.extractor,
                        asset.extractor_version,
                        asset.source_kind.value,
                        asset.source_relative_path,
                        asset.sheet_name,
                        asset.page_number,
                        json.dumps(asset.bbox.__dict__) if asset.bbox else None,
                        asset.source_row_start,
                        asset.source_row_end,
                        asset.source_column_start,
                        asset.source_column_end,
                        asset.row_count,
                        asset.column_count,
                        json.dumps(asset.columns, ensure_ascii=False),
                        asset.raw_artifact_path,
                        asset.normalized_artifact_path,
                        asset.metadata_artifact_path,
                        asset.extraction_confidence,
                        asset.quality_status.value,
                        asset.created_at.replace(tzinfo=None),
                    ],
                )
            for asset in result.text_assets:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO text_assets(
                        text_asset_id, file_id, content_sha256, extraction_run_id,
                        extractor, extractor_version, source_kind, page_number, section,
                        bbox_json, text, language, created_at, source_relative_path,
                        raw_artifact_path, normalized_artifact_path, metadata_artifact_path,
                        is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
                    """,
                    [
                        asset.text_asset_id,
                        asset.file_id,
                        asset.content_sha256,
                        asset.extraction_run_id,
                        asset.extractor,
                        asset.extractor_version,
                        asset.source_kind.value,
                        asset.page_number,
                        asset.section,
                        json.dumps(asset.bbox.__dict__) if asset.bbox else None,
                        asset.text,
                        asset.language,
                        asset.created_at.replace(tzinfo=None),
                        asset.source_relative_path,
                        asset.raw_artifact_path,
                        asset.normalized_artifact_path,
                        asset.metadata_artifact_path,
                    ],
                )
            for chunk in result.text_chunks:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO text_chunks(
                        chunk_id, text_asset_id, file_id, chunk_index, text,
                        char_start, char_end, provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        chunk.chunk_id,
                        chunk.text_asset_id,
                        chunk.file_id,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.char_start,
                        chunk.char_end,
                        json.dumps(chunk.provenance.__dict__, ensure_ascii=False, default=str),
                    ],
                )
            for issue in result.issues:
                connection.execute(
                    """
                    INSERT INTO quality_issues(
                        issue_id, extraction_run_id, asset_id, severity, issue_type,
                        description, evidence_json, detected_by, suggested_action,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                        extraction_run_id=excluded.extraction_run_id,
                        evidence_json=excluded.evidence_json,
                        description=excluded.description
                    """,
                    [
                        issue.issue_id,
                        result.extraction_run_id,
                        issue.asset_id,
                        issue.severity.value,
                        issue.issue_type,
                        issue.description,
                        json.dumps(issue.evidence, ensure_ascii=False, default=str),
                        issue.detected_by,
                        issue.suggested_action,
                        issue.status.value,
                        finished_at,
                    ],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def record_text_result(
        self,
        result: Any,
        *,
        started_at: datetime,
        finished_at: datetime,
        force: bool,
    ) -> None:
        """Persist one deterministic plain-text result through the writer."""

        connection = self.connection
        connection.execute("BEGIN TRANSACTION")
        try:
            if result.status in {"successful", "partial"}:
                connection.execute(
                    """
                    UPDATE text_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor],
                )
            elif result.status == "failed":
                connection.execute(
                    """
                    UPDATE text_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND content_sha256<>? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor, result.source.content_sha256],
                )
            warnings_payload = {"warnings": result.warnings}
            connection.execute(
                """
                INSERT OR REPLACE INTO extraction_runs(
                    extraction_run_id, file_id, content_sha256, extraction_identity,
                    source_root, source_relative_path, started_at, finished_at, status,
                    force, pipeline_version, configuration_version, attempted_route,
                    route_reason, timings_json, warnings_json, extractor_versions_json,
                    table_count, sheet_count, quality_issue_count, total_rows, total_bytes,
                    error_category, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    result.extraction_run_id,
                    result.source.file_id,
                    result.source.content_sha256,
                    result.extraction_identity,
                    result.source.source_root,
                    result.source.relative_path,
                    started_at,
                    finished_at,
                    result.status,
                    force,
                    paths.TEXT_PIPELINE_VERSION,
                    paths.TEXT_CONFIG_VERSION,
                    "text_plain",
                    "supported_txt_text",
                    json.dumps(result.timings.as_dict(), ensure_ascii=False, default=str),
                    json.dumps(warnings_payload, ensure_ascii=False, default=str),
                    json.dumps({result.extractor: result.extractor_version}),
                    0,
                    0,
                    len(result.issues),
                    0,
                    result.source.size_bytes,
                    result.error_category,
                    result.error_message,
                ],
            )
            for asset in result.text_assets:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO text_assets(
                        text_asset_id, file_id, content_sha256, extraction_run_id,
                        extractor, extractor_version, source_kind, page_number, section,
                        bbox_json, text, language, created_at, source_relative_path,
                        raw_artifact_path, normalized_artifact_path, metadata_artifact_path,
                        is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
                    """,
                    [
                        asset.text_asset_id,
                        asset.file_id,
                        asset.content_sha256,
                        asset.extraction_run_id,
                        asset.extractor,
                        asset.extractor_version,
                        asset.source_kind.value,
                        asset.page_number,
                        asset.section,
                        json.dumps(asset.bbox.__dict__) if asset.bbox else None,
                        asset.text,
                        asset.language,
                        asset.created_at.replace(tzinfo=None),
                        asset.source_relative_path,
                        asset.raw_artifact_path,
                        asset.normalized_artifact_path,
                        asset.metadata_artifact_path,
                    ],
                )
            for chunk in result.text_chunks:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO text_chunks(
                        chunk_id, text_asset_id, file_id, chunk_index, text,
                        char_start, char_end, provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        chunk.chunk_id,
                        chunk.text_asset_id,
                        chunk.file_id,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.char_start,
                        chunk.char_end,
                        json.dumps(chunk.provenance.__dict__, ensure_ascii=False, default=str),
                    ],
                )
            for issue in result.issues:
                connection.execute(
                    """
                    INSERT INTO quality_issues(
                        issue_id, extraction_run_id, asset_id, severity, issue_type,
                        description, evidence_json, detected_by, suggested_action,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                        extraction_run_id=excluded.extraction_run_id,
                        evidence_json=excluded.evidence_json,
                        description=excluded.description
                    """,
                    [
                        issue.issue_id,
                        result.extraction_run_id,
                        issue.asset_id,
                        issue.severity.value,
                        issue.issue_type,
                        issue.description,
                        json.dumps(issue.evidence, ensure_ascii=False, default=str),
                        issue.detected_by,
                        issue.suggested_action,
                        issue.status.value,
                        finished_at,
                    ],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def record_pdf_table_result(
        self,
        result: Any,
        *,
        started_at: datetime,
        finished_at: datetime,
        force: bool,
    ) -> None:
        """Persist one candidate PDF table result through the single writer."""

        connection = self.connection
        connection.execute("BEGIN TRANSACTION")
        try:
            if result.status in {"successful", "partial", "deferred_to_ocr"}:
                connection.execute(
                    """
                    UPDATE table_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor],
                )
            elif result.status == "failed":
                connection.execute(
                    """
                    UPDATE table_assets SET is_current=FALSE
                    WHERE file_id=? AND extractor=? AND content_sha256<>? AND is_current=TRUE
                    """,
                    [result.source.file_id, result.extractor, result.source.content_sha256],
                )
            warnings_payload = {
                "warnings": result.warnings,
                "route_reason": result.route_reason,
                "profile_classification": result.profile_classification,
                "pages_attempted": result.pages_attempted,
                "pages_deferred": result.pages_deferred,
                "ground_truth": result.ground_truth_summary,
            }
            connection.execute(
                """
                INSERT OR REPLACE INTO extraction_runs(
                    extraction_run_id, file_id, content_sha256, extraction_identity,
                    source_root, source_relative_path, started_at, finished_at, status,
                    force, pipeline_version, configuration_version, attempted_route,
                    route_reason, timings_json, warnings_json, extractor_versions_json,
                    table_count, sheet_count, quality_issue_count, total_rows, total_bytes,
                    error_category, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    result.extraction_run_id,
                    result.source.file_id,
                    result.source.content_sha256,
                    result.extraction_identity,
                    result.source.source_root,
                    result.source.relative_path,
                    started_at,
                    finished_at,
                    result.status,
                    force,
                    paths.PDF_TABLE_PIPELINE_VERSION,
                    paths.PDF_TABLE_CONFIG_VERSION,
                    "pdf_table_candidate",
                    result.route_reason,
                    json.dumps(result.timings.as_dict()),
                    json.dumps(warnings_payload, ensure_ascii=False, default=str),
                    json.dumps({result.extractor: result.extractor_version}),
                    len(result.assets),
                    result.pages_attempted,
                    len(result.issues),
                    result.total_rows,
                    result.source.size_bytes,
                    result.error_category,
                    result.error_message,
                ],
            )
            for asset in result.assets:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO table_assets(
                        table_id, file_id, content_sha256, extraction_run_id, extractor,
                        extractor_version, source_kind, source_relative_path, sheet_name,
                        page_number, bbox_json, source_row_start, source_row_end,
                        source_column_start, source_column_end, row_count, column_count,
                        columns_json, raw_artifact_path, normalized_artifact_path,
                        metadata_artifact_path, extraction_confidence, quality_status,
                        created_at, is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
                    """,
                    [
                        asset.table_id,
                        asset.file_id,
                        asset.content_sha256,
                        asset.extraction_run_id,
                        asset.extractor,
                        asset.extractor_version,
                        asset.source_kind.value,
                        asset.source_relative_path,
                        asset.sheet_name,
                        asset.page_number,
                        json.dumps(asset.bbox.__dict__) if asset.bbox else None,
                        asset.source_row_start,
                        asset.source_row_end,
                        asset.source_column_start,
                        asset.source_column_end,
                        asset.row_count,
                        asset.column_count,
                        json.dumps(asset.columns, ensure_ascii=False),
                        asset.raw_artifact_path,
                        asset.normalized_artifact_path,
                        asset.metadata_artifact_path,
                        asset.extraction_confidence,
                        asset.quality_status.value,
                        asset.created_at.replace(tzinfo=None),
                    ],
                )
            for issue in result.issues:
                connection.execute(
                    """
                    INSERT INTO quality_issues(
                        issue_id, extraction_run_id, asset_id, severity, issue_type,
                        description, evidence_json, detected_by, suggested_action,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                        extraction_run_id=excluded.extraction_run_id,
                        evidence_json=excluded.evidence_json,
                        description=excluded.description
                    """,
                    [
                        issue.issue_id,
                        result.extraction_run_id,
                        issue.asset_id,
                        issue.severity.value,
                        issue.issue_type,
                        issue.description,
                        json.dumps(issue.evidence, ensure_ascii=False, default=str),
                        issue.detected_by,
                        issue.suggested_action,
                        issue.status.value,
                        finished_at,
                    ],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def record_run_error(self, run_id: str, path: str | None, error_code: str, message: str) -> None:
        self.connection.execute(
            "INSERT INTO run_errors VALUES (?, ?, ?, ?, ?)",
            [run_id, path, error_code, message, utc_now()],
        )

    def record_file_outcome(self, run_id: str, outcome: FileOutcome) -> None:
        connection = self.connection
        plan = _processing_plan(
            file_id=outcome.file_id,
            detected_type=outcome.detection.detected_type,
            mime_like_type=outcome.detection.mime_like_type,
            observed_extension=outcome.observed_extension,
            routing_class=outcome.detection.routing_class,
        )
        existing = connection.execute(
            "SELECT first_seen_run, sha256, last_changed_run FROM files WHERE file_id = ?",
            [outcome.file_id],
        ).fetchone()
        first_seen_run = existing[0] if existing else run_id
        previous_sha = existing[1] if existing else None
        last_changed_run = existing[2] if existing else None
        accepted_sha = outcome.fingerprint.sha256 if outcome.fingerprint.stable else previous_sha
        stat_after = outcome.fingerprint.after or outcome.fingerprint.before
        size_bytes = stat_after.size_bytes if stat_after else None
        mtime_ns = stat_after.mtime_ns if stat_after else None
        if existing and size_bytes is None:
            size_bytes = None
        if existing and mtime_ns is None:
            mtime_ns = None
        if outcome.classification in {"new", "changed"} and (outcome.fingerprint.before or outcome.fingerprint.after):
            last_changed_run = run_id
        error_code = outcome.error_code or outcome.detection.error_code
        error_message = outcome.error_message or outcome.detection.error_message
        # Presence is a filesystem observation, not processing success. A
        # readable/stat-able file remains present even when detection or hash
        # failed; the attempt status and latest_error fields carry that failure.
        presence = "present" if (outcome.fingerprint.before or outcome.fingerprint.after) else "failed"
        updated_at = utc_now()

        connection.execute("BEGIN TRANSACTION")
        try:
            if existing:
                connection.execute(
                    """
                    UPDATE files SET source_root=?, relative_path=?, filename=?, observed_extension=?,
                        size_bytes=?, mtime_ns=?, sha256=?, detected_type=?, mime_like_type=?,
                        detection_method=?, detection_confidence=?, routing_class=?,
                        support_status=?, business_format=?, table_candidate=?, text_candidate=?,
                        may_require_ocr=?, may_require_visual_processing=?, policy_reason=?,
                        current_presence_state=?, last_seen_run=?, last_changed_run=?,
                        latest_error_code=?, latest_error_message=?, last_fingerprint_ms=?,
                        last_detection_ms=?, updated_at=? WHERE file_id=?
                    """,
                    [
                        outcome.source_root,
                        outcome.relative_path,
                        outcome.filename,
                        outcome.observed_extension,
                        size_bytes,
                        mtime_ns,
                        accepted_sha,
                        outcome.detection.detected_type,
                        outcome.detection.mime_like_type,
                        outcome.detection.method,
                        outcome.detection.confidence,
                        outcome.detection.routing_class,
                        plan.support_status.value,
                        plan.business_format.value if plan.business_format else None,
                        plan.attempt_table_extraction,
                        plan.attempt_text_extraction,
                        plan.may_require_ocr,
                        plan.may_require_visual_processing,
                        plan.reason_code,
                        presence,
                        run_id,
                        last_changed_run,
                        error_code,
                        error_message,
                        outcome.fingerprint.elapsed_ms,
                        outcome.detection_ms,
                        updated_at,
                        outcome.file_id,
                    ],
                )
            else:
                connection.execute(
                    """
                    INSERT INTO files(
                        file_id, source_root, relative_path, filename, observed_extension,
                        size_bytes, mtime_ns, sha256, detected_type, mime_like_type,
                        detection_method, detection_confidence, routing_class, support_status,
                        business_format, table_candidate, text_candidate, may_require_ocr,
                        may_require_visual_processing, policy_reason, current_presence_state,
                        first_seen_run, last_seen_run, last_changed_run, latest_error_code,
                        latest_error_message, last_fingerprint_ms, last_detection_ms, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        outcome.file_id,
                        outcome.source_root,
                        outcome.relative_path,
                        outcome.filename,
                        outcome.observed_extension,
                        size_bytes,
                        mtime_ns,
                        accepted_sha,
                        outcome.detection.detected_type,
                        outcome.detection.mime_like_type,
                        outcome.detection.method,
                        outcome.detection.confidence,
                        outcome.detection.routing_class,
                        plan.support_status.value,
                        plan.business_format.value if plan.business_format else None,
                        plan.attempt_table_extraction,
                        plan.attempt_text_extraction,
                        plan.may_require_ocr,
                        plan.may_require_visual_processing,
                        plan.reason_code,
                        presence,
                        first_seen_run,
                        run_id,
                        last_changed_run,
                        error_code,
                        error_message,
                        outcome.fingerprint.elapsed_ms,
                        outcome.detection_ms,
                        updated_at,
                    ],
                )

            if outcome.fingerprint.stable and outcome.fingerprint.sha256:
                content = connection.execute(
                    "SELECT sha256 FROM contents WHERE sha256 = ?",
                    [outcome.fingerprint.sha256],
                ).fetchone()
                if content:
                    connection.execute(
                        "UPDATE contents SET size_bytes=?, last_seen_run=? WHERE sha256=?",
                        [size_bytes, run_id, outcome.fingerprint.sha256],
                    )
                else:
                    connection.execute(
                        "INSERT INTO contents VALUES (?, ?, ?, ?)",
                        [outcome.fingerprint.sha256, size_bytes, run_id, run_id],
                    )

            connection.execute(
                """
                INSERT INTO file_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    outcome.file_id,
                    outcome.source_root,
                    outcome.relative_path,
                    outcome.status,
                    outcome.fingerprint.reused,
                    outcome.fingerprint.before.size_bytes if outcome.fingerprint.before else None,
                    outcome.fingerprint.after.size_bytes if outcome.fingerprint.after else None,
                    outcome.fingerprint.before.mtime_ns if outcome.fingerprint.before else None,
                    outcome.fingerprint.after.mtime_ns if outcome.fingerprint.after else None,
                    outcome.fingerprint.sha256 if outcome.fingerprint.stable else None,
                    outcome.detection.detected_type,
                    outcome.detection.mime_like_type,
                    outcome.detection.method,
                    outcome.detection.confidence,
                    outcome.detection.routing_class,
                    error_code,
                    error_message,
                    outcome.fingerprint.elapsed_ms,
                    outcome.detection_ms,
                    outcome.started_at,
                    outcome.finished_at,
                ],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def mark_missing(self, run_id: str, source_root: str, files: Iterable[ExistingFile]) -> int:
        missing = list(files)
        if not missing:
            return 0
        connection = self.connection
        connection.execute("BEGIN TRANSACTION")
        try:
            for item in missing:
                connection.execute(
                    """
                    UPDATE files SET current_presence_state='missing', last_seen_run=?,
                        latest_error_code=NULL, latest_error_message=NULL, updated_at=?
                    WHERE file_id=?
                    """,
                    [run_id, utc_now(), item.file_id],
                )
                connection.execute(
                    """
                    INSERT INTO file_attempts(
                        run_id, file_id, source_root, relative_path, status, hash_reused,
                        size_before_bytes, size_after_bytes, mtime_before_ns, mtime_after_ns,
                        sha256, detected_type, mime_like_type, detection_method,
                        detection_confidence, routing_class, error_code, error_message,
                        fingerprint_ms, detection_ms, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, 'missing', FALSE, ?, ?, ?, ?, ?,
                              'unknown', 'application/octet-stream', 'not_present', 'low',
                              'unknown', NULL, NULL, 0, 0, ?, ?)
                    """,
                    [
                        run_id,
                        item.file_id,
                        source_root,
                        item.relative_path,
                        item.size_bytes,
                        item.size_bytes,
                        item.mtime_ns,
                        item.mtime_ns,
                        item.sha256,
                        utc_now(),
                        utc_now(),
                    ],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return len(missing)

    def exact_duplicate_paths(self, source_root: str | None = None) -> int:
        clauses = ["sha256 IS NOT NULL", "current_presence_state = 'present'"]
        params: list[Any] = []
        if source_root is not None:
            clauses.append("source_root = ?")
            params.append(source_root)
        where = " AND ".join(clauses)
        row = self.connection.execute(
            f"""
            SELECT COALESCE(SUM(path_count), 0) FROM (
                SELECT sha256, COUNT(*) AS path_count FROM files
                WHERE {where} GROUP BY sha256 HAVING COUNT(*) > 1
            )
            """,
            params,
        ).fetchone()
        return int(row[0] or 0)

    def finish_run(self, summary: ScanSummary, log_path: str | None) -> None:
        self.connection.execute(
            """
            UPDATE scan_runs SET finished_at=?, status=?, discovered_count=?, hashed_count=?,
                reused_hash_count=?, new_count=?, changed_count=?, unchanged_count=?,
                missing_count=?, failed_count=?, discovery_error_count=?, total_bytes=?,
                exact_duplicate_paths=?, elapsed_ms=?, hashing_ms=?, detection_ms=?,
                registry_write_ms=?, log_path=? WHERE run_id=?
            """,
            [
                summary.finished_at,
                summary.status,
                summary.discovered_count,
                summary.hashed_count,
                summary.reused_hash_count,
                summary.new_count,
                summary.changed_count,
                summary.unchanged_count,
                summary.missing_count,
                summary.failed_count,
                summary.discovery_error_count,
                summary.total_bytes,
                summary.exact_duplicate_paths,
                summary.elapsed_ms,
                summary.hashing_ms,
                summary.detection_ms,
                summary.registry_write_ms,
                log_path,
                summary.run_id,
            ],
        )

    def latest_run(self, source_root: str | None = None) -> dict[str, Any] | None:
        if source_root is None:
            cursor = self.connection.execute("SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1")
        else:
            cursor = self.connection.execute(
                "SELECT * FROM scan_runs WHERE source_root = ? ORDER BY started_at DESC LIMIT 1",
                [source_root],
            )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        return dict(zip(columns, row))

    def processing_counts(self, source_root: str | None = None) -> tuple[int, int, int, int]:
        """Return file-oriented processed, failed, deferred and terminal counts.

        The old implementation issued one latest-route query per file.  This
        pivot keeps the same routing semantics in one read-only statement so
        an Overview request does not scale linearly with the number of files.
        """

        source_clause = " AND source_root=?" if source_root is not None else ""
        params = [source_root] if source_root is not None else []
        row = self.connection.execute(
            f"""
            WITH present AS (
                SELECT file_id, sha256, business_format, support_status
                FROM files
                WHERE current_presence_state='present'{source_clause}
            ), ranked AS (
                SELECT e.file_id, e.content_sha256, e.attempted_route, e.status,
                       ROW_NUMBER() OVER (
                           PARTITION BY e.file_id, e.content_sha256, e.attempted_route
                           ORDER BY e.finished_at DESC NULLS LAST,
                                    e.started_at DESC, e.extraction_run_id DESC
                       ) AS row_number
                FROM extraction_runs e
                JOIN present p ON p.file_id=e.file_id AND p.sha256=e.content_sha256
            ), latest AS (
                SELECT file_id, content_sha256, attempted_route, status
                FROM ranked
                WHERE row_number=1
            ), image_ranked AS (
                SELECT e.file_id, e.content_sha256, e.status,
                       ROW_NUMBER() OVER (
                           PARTITION BY e.file_id, e.content_sha256
                           ORDER BY e.finished_at DESC NULLS LAST,
                                    e.started_at DESC, e.extraction_run_id DESC
                       ) AS row_number
                FROM extraction_runs e
                JOIN present p ON p.file_id=e.file_id AND p.sha256=e.content_sha256
                WHERE e.attempted_route IN ('ocr_rapidocr', 'vision_llm')
            ), image_latest AS (
                SELECT file_id, content_sha256, status
                FROM image_ranked
                WHERE row_number=1
            ), pivoted AS (
                SELECT p.file_id, p.support_status, p.business_format, p.sha256,
                       MAX(CASE WHEN l.attempted_route='structured_native' THEN l.status END) AS structured_status,
                       MAX(CASE WHEN l.attempted_route='pdf_native_text' THEN l.status END) AS pdf_native_status,
                       MAX(CASE WHEN l.attempted_route='ocr_rapidocr' THEN l.status END) AS ocr_status,
                       MAX(CASE WHEN l.attempted_route='text_plain' THEN l.status END) AS text_status,
                       MAX(il.status) AS image_status
                FROM present p
                LEFT JOIN latest l ON l.file_id=p.file_id AND l.content_sha256=p.sha256
                LEFT JOIN image_latest il ON il.file_id=p.file_id AND il.content_sha256=p.sha256
                GROUP BY p.file_id, p.support_status, p.business_format, p.sha256
            ), classified AS (
                SELECT CASE
                    WHEN support_status <> 'supported' THEN 'unsupported'
                    WHEN sha256 IS NULL THEN 'failed'
                    WHEN business_format IN ('csv','tsv','xls','xlsx') THEN structured_status
                    WHEN business_format='pdf' THEN CASE
                        WHEN pdf_native_status='failed' OR ocr_status='failed' THEN 'failed'
                        WHEN pdf_native_status IN ('successful','partial') OR ocr_status IN ('successful','partial') THEN 'successful'
                        ELSE COALESCE(pdf_native_status, ocr_status)
                    END
                    WHEN business_format IN ('jpeg','png') THEN image_status
                    WHEN business_format='txt' THEN text_status
                    ELSE NULL
                END AS status
                FROM pivoted
            )
            SELECT
                COALESCE(SUM(CASE WHEN status IN ('successful','partial') THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status NOT IN ('successful','partial','failed','unsupported') OR status IS NULL THEN 1 ELSE 0 END), 0)
            FROM classified
            """,
            params,
        ).fetchone()
        processed = int(row[0] or 0)
        failed = int(row[1] or 0)
        deferred = int(row[2] or 0)
        return processed, failed, deferred, processed + failed

    def catalog_summary(self, source_root: str | None = None) -> dict[str, int]:
        """Return catalog/file/run counters with one aggregate catalog read."""

        catalog_clause = "WHERE source_root=?" if source_root is not None else ""
        file_clause = " AND source_root=?" if source_root is not None else ""
        params = ([source_root] if source_root is not None else []) * 2
        aggregate_cursor = self.connection.execute(
            f"""
            WITH catalog AS (
                SELECT * FROM catalog_assets {catalog_clause}
            ), asset_stats AS (
                SELECT
                    COUNT(*) FILTER (WHERE asset_type='table') AS table_assets,
                    COALESCE(SUM(CASE WHEN asset_type='table' THEN "rows" ELSE 0 END), 0) AS table_rows,
                    COUNT(*) FILTER (WHERE asset_type='text') AS text_assets,
                    COALESCE(SUM(CASE WHEN asset_type='text' THEN chunks ELSE 0 END), 0) AS text_chunks,
                    COUNT(*) FILTER (WHERE quality_status='ready') AS ready,
                    COUNT(*) FILTER (WHERE quality_status='needs_review') AS needs_review,
                    COUNT(*) FILTER (WHERE quality_status='unusable') AS unusable,
                    COUNT(*) FILTER (WHERE semantic_status='pending') AS semantic_pending,
                    COUNT(*) FILTER (WHERE semantic_status='enriched') AS semantic_enriched,
                    COUNT(*) FILTER (WHERE cleaning_status='successful') AS cleaning_successful,
                    COUNT(*) FILTER (WHERE cleaning_status='failed') AS cleaning_failed,
                    COUNT(*) FILTER (WHERE cleaning_status='not_run') AS cleaning_not_run
                FROM catalog
            ), file_stats AS (
                SELECT
                    COUNT(*) AS files,
                    COUNT(*) FILTER (WHERE support_status='supported') AS supported_files,
                    COUNT(*) FILTER (WHERE support_status<>'supported' OR support_status IS NULL) AS unsupported_files
                FROM files
                WHERE current_presence_state='present'{file_clause}
            ), issue_stats AS (
                SELECT
                    COUNT(*) FILTER (WHERE q.status IN ('open','accepted')) AS quality_issues,
                    COUNT(*) FILTER (WHERE q.status='open') AS open_quality_issues
                FROM quality_issues q
                WHERE q.issue_type <> 'possible_table_candidate'
                  AND EXISTS (
                      SELECT 1 FROM catalog c
                      WHERE c.asset_id=q.asset_id
                        AND (q.cleaning_run_id IS NULL OR q.cleaning_run_id=c.cleaning_run_id)
                  )
            )
            SELECT * FROM file_stats, asset_stats, issue_stats
            """,
            params,
        )
        aggregate = aggregate_cursor.fetchone()
        columns = [item[0] for item in aggregate_cursor.description] if aggregate else []
        values = dict(zip(columns, aggregate)) if aggregate else {}
        run_clause = "WHERE source_root=?" if source_root is not None else ""
        run_rows = self.connection.execute(
            f"SELECT status, COUNT(*) FROM extraction_runs {run_clause} GROUP BY status",
            [source_root] if source_root is not None else [],
        ).fetchall()
        result = {f"extraction_{status}": int(count) for status, count in run_rows}
        result.update({key: int(value or 0) for key, value in values.items()})
        result["catalog_assets"] = result.get("table_assets", 0) + result.get("text_assets", 0)
        return result

    def list_catalog_assets(
        self,
        *,
        source_root: str | None = None,
        asset_type: str | None = None,
        quality_status: str | None = None,
        source_format: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_total: bool = False,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100_000:
            raise ValueError("limit must be between 1 and 100000")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        clauses = ["1=1"]
        params: list[Any] = []
        for column, value in (
            ("source_root", source_root),
            ("asset_type", asset_type),
            ("quality_status", quality_status),
            ("source_format", source_format),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if query is not None and query.strip():
            clauses.append(
                "(effective_display_name ILIKE ? OR fallback_display_name ILIKE ? OR source_file ILIKE ?)"
            )
            needle = f"%{query.strip()}%"
            params.extend((needle, needle, needle))
        projection = "c.*, COUNT(*) OVER() AS _catalog_total" if include_total else "c.*"
        cursor = self.connection.execute(
            f"SELECT {projection} FROM catalog_assets c WHERE {' AND '.join(clauses)} ORDER BY source_file, asset_type, asset_id LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def count_catalog_assets(
        self,
        *,
        source_root: str | None = None,
        asset_type: str | None = None,
        quality_status: str | None = None,
        source_format: str | None = None,
        query: str | None = None,
    ) -> int:
        """Count catalog rows using the same bounded metadata filters as listing."""

        clauses = ["1=1"]
        params: list[Any] = []
        for column, value in (
            ("source_root", source_root),
            ("asset_type", asset_type),
            ("quality_status", quality_status),
            ("source_format", source_format),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if query is not None and query.strip():
            clauses.append(
                "(effective_display_name ILIKE ? OR fallback_display_name ILIKE ? OR source_file ILIKE ?)"
            )
            needle = f"%{query.strip()}%"
            params.extend((needle, needle, needle))
        row = self.connection.execute(
            f"SELECT COUNT(*) FROM catalog_assets WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return int(row[0] or 0)

    def list_quality_issues(
        self,
        *,
        status: str | None = None,
        severity: str | None = None,
        asset_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100_000:
            raise ValueError("limit must be between 1 and 100000")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        clauses = ["1=1", "q.issue_type <> 'possible_table_candidate'"]
        params: list[Any] = []
        for column, value in (("q.status", status), ("q.severity", severity), ("q.asset_id", asset_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        cursor = self.connection.execute(
            f"""
            SELECT q.issue_id, q.extraction_run_id, q.cleaning_run_id, q.semantic_run_id,
                   q.asset_id, q.severity, q.issue_type, q.description, q.evidence_json,
                   q.detected_by, q.suggested_action, q.status, q.created_at,
                   c.asset_type, c.effective_display_name, c.fallback_display_name,
                   c.source_file, c.source_format
            FROM quality_issues q
            LEFT JOIN catalog_assets c ON c.asset_id=q.asset_id
            WHERE {' AND '.join(clauses)}
            ORDER BY q.created_at DESC, q.issue_id
            LIMIT ? OFFSET ?
            """,
            [*params, int(limit), int(offset)],
        )
        columns = [item[0] for item in cursor.description]
        result: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            item = dict(zip(columns, row))
            evidence = item.pop("evidence_json", None)
            if isinstance(evidence, str):
                try:
                    item["evidence"] = json.loads(evidence)
                except json.JSONDecodeError:
                    item["evidence"] = evidence
            else:
                item["evidence"] = evidence
            result.append(item)
        return result

    def count_quality_issues(
        self,
        *,
        status: str | None = None,
        severity: str | None = None,
        asset_id: str | None = None,
    ) -> int:
        clauses = ["1=1", "issue_type <> 'possible_table_candidate'"]
        params: list[Any] = []
        for column, value in (("status", status), ("severity", severity), ("asset_id", asset_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        row = self.connection.execute(
            f"SELECT COUNT(*) FROM quality_issues WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return int(row[0] or 0)

    def visible_quality_issue_counts(self, assets: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Count user-visible open/accepted issues for a catalog page.

        ``catalog_assets`` is a versioned view, so an existing workspace may
        still have the previous view definition until an explicit migration.
        Keep this small, DDL-free correction at the service boundary so both
        fresh and existing registries hide routing hints consistently.
        """

        selected = [
            (str(row.get("asset_id") or ""), row.get("cleaning_run_id"))
            for row in assets
            if row.get("asset_id")
        ]
        if not selected:
            return {}
        values = ", ".join("(?, ?)" for _ in selected)
        params: list[Any] = []
        for asset_id, cleaning_run_id in selected:
            params.extend((asset_id, cleaning_run_id))
        cursor = self.connection.execute(
            f"""
            WITH selected(asset_id, cleaning_run_id) AS (VALUES {values})
            SELECT selected.asset_id, COUNT(q.issue_id) AS issue_count
            FROM selected
            LEFT JOIN quality_issues q
              ON q.asset_id=selected.asset_id
             AND q.issue_type <> 'possible_table_candidate'
             AND q.status IN ('open', 'accepted')
             AND (q.cleaning_run_id IS NULL OR q.cleaning_run_id=selected.cleaning_run_id)
            GROUP BY selected.asset_id
            """,
            params,
        )
        return {str(asset_id): int(count or 0) for asset_id, count in cursor.fetchall()}

    def update_quality_issue_status(self, issue_id: str, status: str) -> dict[str, Any] | None:
        if status not in {"open", "accepted", "ignored", "resolved"}:
            raise ValueError("quality issue status must be open, accepted, ignored, or resolved")
        exists = self.connection.execute(
            "SELECT issue_id FROM quality_issues WHERE issue_id=?",
            [issue_id],
        ).fetchone()
        if exists is None:
            return None
        self.connection.execute(
            "UPDATE quality_issues SET status=? WHERE issue_id=?",
            [status, issue_id],
        )
        cursor = self.connection.execute(
            "SELECT issue_id, asset_id, severity, issue_type, description, evidence_json, detected_by, suggested_action, status, created_at FROM quality_issues WHERE issue_id=?",
            [issue_id],
        )
        columns = [item[0] for item in cursor.description]
        row = cursor.fetchone()
        if row is None:
            return None
        item = dict(zip(columns, row))
        evidence = item.pop("evidence_json", None)
        if isinstance(evidence, str):
            try:
                item["evidence"] = json.loads(evidence)
            except json.JSONDecodeError:
                item["evidence"] = evidence
        else:
            item["evidence"] = evidence
        return item

    def catalog_asset_details(self, asset_id: str) -> dict[str, Any] | None:
        cursor = self.connection.execute("SELECT * FROM catalog_assets WHERE asset_id=?", [asset_id])
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        result: dict[str, Any] = dict(zip(columns, row))
        issue_cursor = self.connection.execute(
            """
            SELECT issue_id, extraction_run_id, cleaning_run_id, semantic_run_id, asset_id, severity,
                   issue_type, description, evidence_json, detected_by,
                   suggested_action, status, created_at
            FROM quality_issues
            WHERE asset_id=? AND issue_type <> 'possible_table_candidate'
              AND status IN ('open', 'accepted')
              AND (cleaning_run_id IS NULL OR cleaning_run_id=?)
            ORDER BY created_at, issue_id
            """,
            [asset_id, result.get("cleaning_run_id")],
        )
        issue_columns = [item[0] for item in issue_cursor.description]
        issues = []
        for issue_row in issue_cursor.fetchall():
            issue = dict(zip(issue_columns, issue_row))
            evidence = issue.get("evidence_json")
            if isinstance(evidence, str):
                try:
                    issue["evidence"] = json.loads(evidence)
                except json.JSONDecodeError:
                    issue["evidence"] = evidence
            else:
                issue["evidence"] = evidence
            issue.pop("evidence_json", None)
            issues.append(issue)
        result["quality_issues"] = issues
        result["quality_issue_count"] = len(issues)
        profile_path = result.get("profile_artifact_path")
        if profile_path:
            profile_row = self.connection.execute(
                "SELECT profile_json FROM table_profiles WHERE profile_artifact_path=? UNION ALL SELECT profile_json FROM text_profiles WHERE profile_artifact_path=? LIMIT 1",
                [profile_path, profile_path],
            ).fetchone()
            if profile_row:
                profile = profile_row[0]
                if isinstance(profile, str):
                    try:
                        profile = json.loads(profile)
                    except json.JSONDecodeError:
                        pass
                result["profile"] = profile
        result["semantic_history"] = self.semantic_history(asset_id)
        return result

    def semantic_reusable(self, semantic_identity: str, asset_id: str, asset_type: str) -> dict[str, Any] | None:
        """Return a successful semantic result for an exact input identity."""

        cursor = self.connection.execute(
            """
            SELECT r.*, m.display_name, m.category, m.description, m.keywords_json,
                   m.summary, m.semantic_fields_json, m.confidence, m.generated_at
            FROM semantic_runs r
            JOIN semantic_metadata m ON m.semantic_run_id = r.semantic_run_id
            WHERE r.semantic_identity=? AND r.asset_id=? AND r.asset_type=?
              AND r.status='successful'
            ORDER BY r.finished_at DESC, r.semantic_run_id DESC
            LIMIT 1
            """,
            [semantic_identity, asset_id, asset_type],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        return dict(zip(columns, row))

    def recover_incomplete_semantic_runs(self, asset_id: str | None = None) -> int:
        clauses = ["status='running'"]
        params: list[Any] = []
        if asset_id is not None:
            clauses.append("asset_id=?")
            params.append(asset_id)
        now = utc_now()

        def recover() -> int:
            cursor = self.connection.execute(
                f"UPDATE semantic_runs SET status='failed', finished_at=?, error_code='interrupted', error_message='semantic run interrupted before completion' WHERE {' AND '.join(clauses)} RETURNING semantic_run_id",
                [now, *params],
            )
            return len(cursor.fetchall())

        return self._run_recovery_with_index_repair("semantic_runs", recover)

    def start_semantic_run(
        self,
        *,
        semantic_run_id: str,
        semantic_identity: str,
        asset_id: str,
        asset_type: str,
        file_id: str,
        content_sha256: str,
        normalized_artifact_identity: str,
        model: str,
        prompt_version: str,
        config_version: str,
        input_hash: str,
        provider: str,
        input_metadata: Mapping[str, Any],
        started_at: datetime,
        force: bool = False,
    ) -> None:
        self.recover_incomplete_semantic_runs(asset_id)
        self.connection.execute(
            """
            INSERT INTO semantic_runs(
                semantic_run_id, semantic_identity, asset_id, asset_type, file_id,
                content_sha256, normalized_artifact_identity, model, prompt_version,
                config_version, input_hash, provider, status, started_at, current,
                input_metadata_json, warnings_json, error_code, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, FALSE, ?, NULL, NULL, NULL)
            """,
            [
                semantic_run_id,
                semantic_identity,
                asset_id,
                asset_type,
                file_id,
                content_sha256,
                normalized_artifact_identity,
                model,
                prompt_version,
                config_version,
                input_hash,
                provider,
                started_at,
                json.dumps({**dict(input_metadata), "force": bool(force)}, ensure_ascii=False, default=str),
            ],
        )

    def record_semantic_failure(
        self,
        semantic_run_id: str,
        *,
        finished_at: datetime,
        error_code: str,
        error_message: str,
        warnings: Iterable[str] = (),
    ) -> None:
        self.connection.execute(
            """
            UPDATE semantic_runs SET status='failed', finished_at=?, current=FALSE,
                warnings_json=?, error_code=?, error_message=?
            WHERE semantic_run_id=?
            """,
            [finished_at, json.dumps(list(warnings), ensure_ascii=False), error_code, error_message, semantic_run_id],
        )

    def record_semantic_success(
        self,
        *,
        semantic_run_id: str,
        asset_id: str,
        asset_type: str,
        metadata: Mapping[str, Any],
        generated_at: datetime,
        finished_at: datetime,
        warnings: Iterable[str] = (),
        suggestions: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        """Atomically publish semantic metadata and review-only suggestions."""

        connection = self.connection
        warning_list = list(warnings)
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                "UPDATE semantic_runs SET current=FALSE WHERE asset_id=? AND asset_type=? AND current=TRUE",
                [asset_id, asset_type],
            )
            connection.execute(
                "UPDATE semantic_metadata SET current=FALSE WHERE asset_id=? AND asset_type=? AND current=TRUE",
                [asset_id, asset_type],
            )
            connection.execute(
                """
                INSERT INTO semantic_metadata(
                    asset_id, asset_type, display_name, category, description,
                    keywords_json, summary, semantic_fields_json, model,
                    prompt_version, confidence, generated_at, semantic_run_id,
                    input_hash, current
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, model, prompt_version, ?, ?, ?, input_hash, TRUE
                FROM semantic_runs WHERE semantic_run_id=?
                """,
                [
                    asset_id,
                    asset_type,
                    str(metadata["display_name"]),
                    str(metadata["category"]),
                    str(metadata["description"]),
                    json.dumps(metadata.get("keywords", []), ensure_ascii=False, default=str),
                    str(metadata["summary"]),
                    json.dumps(metadata.get("semantic_fields", []), ensure_ascii=False, default=str),
                    float(metadata["confidence"]),
                    generated_at,
                    semantic_run_id,
                    semantic_run_id,
                ],
            )
            connection.execute(
                """
                UPDATE semantic_runs SET status='successful', finished_at=?, current=TRUE,
                    warnings_json=?, error_code=NULL, error_message=NULL
                WHERE semantic_run_id=?
                """,
                [finished_at, json.dumps(warning_list, ensure_ascii=False), semantic_run_id],
            )
            for suggestion in suggestions:
                issue_type = str(suggestion.get("issue_type") or "semantic_quality_suggestion")
                explanation = str(suggestion.get("explanation") or "Semantic provider supplied a review suggestion.")
                suggested_action = str(suggestion.get("suggested_action") or "Review this suggestion; it is not automatically applied.")
                severity = str(suggestion.get("severity") or "warning")
                if severity not in {"info", "warning", "error", "critical"}:
                    severity = "warning"
                issue_identity = json.dumps(
                    [semantic_run_id, asset_id, issue_type, explanation, suggested_action],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
                issue_id = f"sem_issue_{hashlib.sha256(issue_identity).hexdigest()[:32]}"
                connection.execute(
                    """
                    INSERT OR REPLACE INTO quality_issues(
                        issue_id, extraction_run_id, cleaning_run_id, semantic_run_id,
                        asset_id, severity, issue_type, description, evidence_json,
                        detected_by, suggested_action, status, created_at
                    ) VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                    """,
                    [
                        issue_id,
                        semantic_run_id,
                        asset_id,
                        severity,
                        issue_type,
                        explanation,
                        json.dumps({"semantic": True, "provider_suggestion": dict(suggestion)}, ensure_ascii=False, default=str),
                        "semantic",
                        suggested_action,
                        finished_at,
                    ],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def semantic_history(self, asset_id: str) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            "SELECT * FROM semantic_runs WHERE asset_id=? ORDER BY started_at, semantic_run_id",
            [asset_id],
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def list_files(self, source_root: str | None = None, state: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100_000:
            raise ValueError("limit must be between 1 and 100000")
        clauses: list[str] = []
        params: list[Any] = []
        if source_root is not None:
            clauses.append("source_root = ?")
            params.append(source_root)
        if state is not None:
            clauses.append("current_presence_state = ?")
            params.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = self.connection.execute(
            f"SELECT file_id, source_root, relative_path, filename, observed_extension, size_bytes, mtime_ns, sha256, detected_type, mime_like_type, detection_method, detection_confidence, routing_class, support_status, business_format, table_candidate, text_candidate, may_require_ocr, may_require_visual_processing, policy_reason, current_presence_state, first_seen_run, last_seen_run, last_changed_run, latest_error_code, latest_error_message FROM files {where} ORDER BY source_root, relative_path LIMIT {int(limit)}",
            params,
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
