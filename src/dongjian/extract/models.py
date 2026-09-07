"""Internal contracts shared by structured extractors and the coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dongjian.assets import QualityIssue, TableAsset


@dataclass(frozen=True)
class StructuredSource:
    file_id: str
    content_sha256: str
    source_root: str
    relative_path: str
    business_format: str
    size_bytes: int
    mtime_ns: int
    workspace_root: Path

    @property
    def path(self) -> Path:
        return Path(self.source_root) / Path(self.relative_path)


@dataclass
class StageTimings:
    workbook_open_ms: float = 0.0
    extraction_ms: float = 0.0
    normalization_ms: float = 0.0
    parquet_write_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "workbook_open_ms": self.workbook_open_ms,
            "extraction_ms": self.extraction_ms,
            "normalization_ms": self.normalization_ms,
            "parquet_write_ms": self.parquet_write_ms,
        }


@dataclass
class FileExtractionResult:
    source: StructuredSource
    extraction_run_id: str
    extraction_identity: str
    extractor: str
    extractor_version: str
    status: str = "successful"
    assets: list[TableAsset] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    sheet_count: int = 0
    timings: StageTimings = field(default_factory=StageTimings)
    error_category: str | None = None
    error_message: str | None = None

    @property
    def total_rows(self) -> int:
        return sum(asset.row_count for asset in self.assets)


@dataclass
class StructuredExtractionSummary:
    source_root: str
    files_considered: int = 0
    structured_supported: int = 0
    extracted: int = 0
    reused: int = 0
    tables_produced: int = 0
    quality_issues: int = 0
    failed: int = 0
    sheets: int = 0
    total_rows: int = 0
    total_bytes: int = 0
    discovery_scan_ms: float = 0.0
    workbook_open_ms: float = 0.0
    extraction_ms: float = 0.0
    normalization_ms: float = 0.0
    parquet_write_ms: float = 0.0
    registry_write_ms: float = 0.0
    wall_time_ms: float = 0.0

    def benchmark_metrics(self) -> dict[str, float | int | str]:
        seconds = self.wall_time_ms / 1000.0 if self.wall_time_ms else 0.0
        mb = self.total_bytes / (1024 * 1024)
        return {
            "files": self.structured_supported,
            "sheets": self.sheets,
            "table_assets": self.tables_produced,
            "rows": self.total_rows,
            "bytes": self.total_bytes,
            "files_per_sec": self.structured_supported / seconds if seconds else 0.0,
            "rows_per_sec": self.total_rows / seconds if seconds else 0.0,
            "mb_per_sec": mb / seconds if seconds else 0.0,
            "extraction_ms": self.extraction_ms,
            "normalization_ms": self.normalization_ms,
            "parquet_write_ms": self.parquet_write_ms,
            "registry_write_ms": self.registry_write_ms,
            "wall_time_ms": self.wall_time_ms,
            "peak_memory": "not collected (no zero-cost reliable cross-process metric)",
        }
