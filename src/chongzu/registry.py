"""Versioned DuckDB registry and its single-writer operations."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

import duckdb

from . import paths
from .processing_policy import ProcessingPlan, RegistryFileInfo, plan_processing
from .types import ExistingFile, FileOutcome, ScanSummary


class RegistryError(RuntimeError):
    """Raised when the registry cannot be initialized or updated safely."""


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
        PRIMARY KEY(asset_id, asset_type, model, prompt_version, generated_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quality_issues (
        issue_id VARCHAR PRIMARY KEY,
        extraction_run_id VARCHAR,
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
    "CREATE INDEX IF NOT EXISTS idx_quality_issues_asset ON quality_issues(asset_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_extraction_runs_file ON extraction_runs(file_id, content_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_extraction_identity ON extraction_runs(extraction_identity, status)",
)


SCHEMA_STATEMENTS = CORE_SCHEMA_STATEMENTS + CATALOG_SCHEMA_STATEMENTS


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


def initialize_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS registry_meta (
            meta_key VARCHAR PRIMARY KEY,
            meta_value VARCHAR NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    row = connection.execute(
        "SELECT meta_value FROM registry_meta WHERE meta_key = ?",
        [paths.REGISTRY_SCHEMA_NAME],
    ).fetchone()
    if row is None:
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
    if version == 3 and paths.REGISTRY_SCHEMA_VERSION == 3:
        # The v2->v3 DDL is idempotent. Rechecking v3 also makes an interrupted
        # ALTER sequence restartable before any coordinator uses the catalog.
        _migrate_v2_to_v3(connection)
    if version < paths.REGISTRY_SCHEMA_VERSION:
        raise RegistryError(f"Registry schema migration from {version} to {paths.REGISTRY_SCHEMA_VERSION} is not implemented")


class Registry:
    """A DuckDB connection owned by the coordinator/writer thread."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, path: Path):
        self.connection = connection
        self.path = path

    @classmethod
    def open(cls, path: Path | str | None = None) -> "Registry":
        registry_path = Path(path or paths.REGISTRY_PATH).resolve()
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = duckdb.connect(str(registry_path))
            initialize_schema(connection)
        except Exception as exc:  # noqa: BLE001 - convert DB driver errors at the boundary
            raise RegistryError(f"Unable to initialize registry {registry_path}: {exc}") from exc
        return cls(connection, registry_path)

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
            [utc_now(), source_root],
        )
        return count

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

    def recover_incomplete_extractions(self, source_root: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE source_root=? AND status='running'",
            [source_root],
        ).fetchone()
        count = int(row[0] or 0)
        self.connection.execute(
            "UPDATE extraction_runs SET status='interrupted', finished_at=? WHERE source_root=? AND status='running'",
            [utc_now(), source_root],
        )
        return count

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
            warnings_payload = {
                "warnings": result.warnings,
                "ocr_targets": result.ocr_targets,
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
                    json.dumps({result.extractor: result.extractor_version}),
                    0,
                    result.target_count,
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

    def catalog_summary(self, source_root: str | None = None) -> dict[str, int]:
        run_where = "WHERE source_root=?" if source_root is not None else ""
        params = [source_root] if source_root is not None else []
        run_rows = self.connection.execute(
            f"SELECT status, COUNT(*) FROM extraction_runs {run_where} GROUP BY status",
            params,
        ).fetchall()
        asset_where = "AND f.source_root=?" if source_root is not None else ""
        asset_params = [source_root] if source_root is not None else []
        assets = self.connection.execute(
            f"""
            SELECT COUNT(*), COALESCE(SUM(t.row_count), 0)
            FROM table_assets t JOIN files f ON f.file_id=t.file_id
            WHERE t.is_current=TRUE {asset_where}
            """,
            asset_params,
        ).fetchone()
        text_assets = self.connection.execute(
            f"""
            SELECT COUNT(*) FROM text_assets t JOIN files f ON f.file_id=t.file_id
            WHERE t.is_current=TRUE {asset_where}
            """,
            asset_params,
        ).fetchone()
        text_chunks = self.connection.execute(
            f"""
            SELECT COUNT(*) FROM text_chunks c
            JOIN text_assets t ON t.text_asset_id=c.text_asset_id
            JOIN files f ON f.file_id=t.file_id
            WHERE t.is_current=TRUE {asset_where}
            """,
            asset_params,
        ).fetchone()
        issues = self.connection.execute(
            f"""
            SELECT COUNT(*) FROM quality_issues q
            JOIN extraction_runs r ON r.extraction_run_id=q.extraction_run_id
            {run_where}
            """,
            params,
        ).fetchone()
        result = {f"extraction_{status}": int(count) for status, count in run_rows}
        result.update(
            {
                "table_assets": int(assets[0] or 0),
                "table_rows": int(assets[1] or 0),
                "text_assets": int(text_assets[0] or 0),
                "text_chunks": int(text_chunks[0] or 0),
                "quality_issues": int(issues[0] or 0),
            }
        )
        return result

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
