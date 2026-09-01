"""Shared data structures for discovery, detection, hashing, and registry writes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FileStat:
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class FingerprintResult:
    sha256: str | None
    before: FileStat | None
    after: FileStat | None
    stable: bool
    elapsed_ms: float
    error_code: str | None = None
    error_message: str | None = None
    reused: bool = False


@dataclass(frozen=True)
class DetectionResult:
    detected_type: str
    mime_like_type: str
    method: str
    confidence: str
    routing_class: str
    evidence: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path
    relative_path: str
    filename: str
    observed_extension: str
    is_symlink: bool = False


@dataclass(frozen=True)
class DiscoveryIssue:
    path: str
    code: str
    message: str
    is_error: bool = True


@dataclass(frozen=True)
class ExistingFile:
    file_id: str
    relative_path: str
    size_bytes: int | None
    mtime_ns: int | None
    sha256: str | None
    current_presence_state: str
    first_seen_run: str
    last_changed_run: str | None


@dataclass(frozen=True)
class FileOutcome:
    file_id: str
    source_root: str
    relative_path: str
    filename: str
    observed_extension: str
    status: str
    classification: str
    detection: DetectionResult
    fingerprint: FingerprintResult
    started_at: datetime
    finished_at: datetime
    detection_ms: float
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class ScanSummary:
    run_id: str
    source_root: str
    started_at: datetime
    finished_at: datetime
    status: str
    discovered_count: int = 0
    hashed_count: int = 0
    reused_hash_count: int = 0
    new_count: int = 0
    changed_count: int = 0
    unchanged_count: int = 0
    missing_count: int = 0
    failed_count: int = 0
    discovery_error_count: int = 0
    total_bytes: int = 0
    exact_duplicate_paths: int = 0
    elapsed_ms: float = 0.0
    hashing_ms: float = 0.0
    detection_ms: float = 0.0
    registry_write_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["started_at"] = self.started_at.isoformat()
        values["finished_at"] = self.finished_at.isoformat()
        return values

