"""Versioned DuckDB registry and its single-writer operations."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

import duckdb

from . import paths
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


SCHEMA_STATEMENTS = (
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

    def record_run_error(self, run_id: str, path: str | None, error_code: str, message: str) -> None:
        self.connection.execute(
            "INSERT INTO run_errors VALUES (?, ?, ?, ?, ?)",
            [run_id, path, error_code, message, utc_now()],
        )

    def record_file_outcome(self, run_id: str, outcome: FileOutcome) -> None:
        connection = self.connection
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
                    INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            f"SELECT file_id, source_root, relative_path, filename, observed_extension, size_bytes, mtime_ns, sha256, detected_type, mime_like_type, detection_method, detection_confidence, routing_class, current_presence_state, first_seen_run, last_seen_run, last_changed_run, latest_error_code, latest_error_message FROM files {where} ORDER BY source_root, relative_path LIMIT {int(limit)}",
            params,
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
