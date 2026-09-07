"""Small contracts shared by the Phase 6 cleaning workers and registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chongzu.assets import QualityIssue


@dataclass
class CleaningTimings:
    normalize_ms: float = 0.0
    profile_ms: float = 0.0
    parquet_write_ms: float = 0.0
    artifact_write_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "normalize_ms": self.normalize_ms,
            "profile_ms": self.profile_ms,
            "parquet_write_ms": self.parquet_write_ms,
            "artifact_write_ms": self.artifact_write_ms,
        }


@dataclass
class CleaningAssetResult:
    asset_id: str
    asset_type: str
    file_id: str
    content_sha256: str
    source_root: str
    source_relative_path: str
    raw_artifact_identity: str
    cleaner: str
    cleaner_version: str
    config_version: str
    cleaning_run_id: str
    cleaning_identity: str
    status: str = "successful"
    normalized_artifact_path: str | None = None
    manifest_artifact_path: str | None = None
    profile_artifact_path: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    profile_row: dict[str, Any] = field(default_factory=dict)
    quality_status: str = "needs_review"
    issues: list[QualityIssue] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    timings: CleaningTimings = field(default_factory=CleaningTimings)
    error_category: str | None = None
    error_message: str | None = None


@dataclass
class CleaningSummary:
    source_root: str
    files_discovered: int = 0
    files_supported: int = 0
    files_unsupported: int = 0
    extracted: int = 0
    reused_extraction: int = 0
    extraction_failures: int = 0
    table_assets: int = 0
    text_assets: int = 0
    cleaned: int = 0
    reused_cleaning: int = 0
    cleaning_failures: int = 0
    ready: int = 0
    needs_review: int = 0
    unusable: int = 0
    quality_issues: int = 0
    semantic_pending: int = 0
    rows: int = 0
    chars: int = 0
    normalize_ms: float = 0.0
    profile_ms: float = 0.0
    parquet_write_ms: float = 0.0
    artifact_write_ms: float = 0.0
    duckdb_write_ms: float = 0.0
    cleaning_wall_time_ms: float = 0.0
    wall_time_ms: float = 0.0
    scan_metrics: dict[str, Any] = field(default_factory=dict)

    def benchmark_metrics(self) -> dict[str, float | int | str]:
        seconds = self.wall_time_ms / 1000.0 if self.wall_time_ms else 0.0
        return {
            "table assets": self.table_assets,
            "rows": self.rows,
            "text assets": self.text_assets,
            "chars": self.chars,
            "cleaned": self.cleaned,
            "reused": self.reused_cleaning,
            "extraction failures": self.extraction_failures,
            "profile ms": self.profile_ms,
            "normalize ms": self.normalize_ms,
            "parquet write ms": self.parquet_write_ms,
            "DuckDB write ms": self.duckdb_write_ms,
            "wall time ms": self.wall_time_ms,
            "rows/sec": self.rows / seconds if seconds else 0.0,
        }
