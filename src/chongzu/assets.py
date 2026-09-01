"""Canonical extraction, semantic, quality, and provenance contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from typing import Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AssetType(str, Enum):
    TABLE = "table"
    TEXT = "text"


class SourceKind(str, Enum):
    FILE = "file"
    SHEET = "sheet"
    PAGE = "page"
    IMAGE = "image"
    SLIDE = "slide"
    SECTION = "section"


class AssetQualityStatus(str, Enum):
    NOT_ASSESSED = "not_assessed"
    PASS = "pass"
    REVIEW = "review"
    FAIL = "fail"


class QualityIssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class QualityIssueStatus(str, Enum):
    OPEN = "open"
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class BoundingBox:
    """Coordinates in the extractor's documented source coordinate system."""

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        values = (self.x0, self.y0, self.x1, self.y1)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("bbox coordinates must be finite")
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bbox maximum coordinates must not precede minimum coordinates")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _required(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _sha256(value: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("content_sha256 must be a lower-case hexadecimal SHA-256")


def _confidence(value: float | None) -> None:
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be between 0 and 1")


def _timestamp(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")


def _stable_id(prefix: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:32]}"


def make_table_id(
    *,
    file_id: str,
    content_sha256: str,
    extractor: str,
    extractor_version: str,
    source_kind: SourceKind | str,
    source_locator: str,
    asset_index: int,
) -> str:
    """Create a stable table ID exclusively from extraction provenance."""

    for value, name in (
        (file_id, "file_id"),
        (extractor, "extractor"),
        (extractor_version, "extractor_version"),
        (source_locator, "source_locator"),
    ):
        _required(value, name)
    _sha256(content_sha256)
    if asset_index < 0:
        raise ValueError("asset_index must be non-negative")
    return _stable_id(
        "tbl",
        {
            "asset_index": asset_index,
            "content_sha256": content_sha256,
            "extractor": extractor,
            "extractor_version": extractor_version,
            "file_id": file_id,
            "source_kind": SourceKind(source_kind).value,
            "source_locator": source_locator,
        },
    )


def make_text_asset_id(
    *,
    file_id: str,
    content_sha256: str,
    extractor: str,
    extractor_version: str,
    source_kind: SourceKind | str,
    source_locator: str,
    asset_index: int,
) -> str:
    """Create a stable text ID exclusively from extraction provenance."""

    for value, name in (
        (file_id, "file_id"),
        (extractor, "extractor"),
        (extractor_version, "extractor_version"),
        (source_locator, "source_locator"),
    ):
        _required(value, name)
    _sha256(content_sha256)
    if asset_index < 0:
        raise ValueError("asset_index must be non-negative")
    return _stable_id(
        "txt",
        {
            "asset_index": asset_index,
            "content_sha256": content_sha256,
            "extractor": extractor,
            "extractor_version": extractor_version,
            "file_id": file_id,
            "source_kind": SourceKind(source_kind).value,
            "source_locator": source_locator,
        },
    )


def make_chunk_id(*, text_asset_id: str, chunk_index: int, char_start: int, char_end: int) -> str:
    _required(text_asset_id, "text_asset_id")
    if chunk_index < 0 or char_start < 0 or char_end < char_start:
        raise ValueError("chunk identity offsets must be ordered and non-negative")
    return _stable_id(
        "chk",
        {
            "char_end": char_end,
            "char_start": char_start,
            "chunk_index": chunk_index,
            "text_asset_id": text_asset_id,
        },
    )


@dataclass(frozen=True)
class TableAsset:
    table_id: str
    file_id: str
    content_sha256: str
    extraction_run_id: str
    extractor: str
    extractor_version: str
    source_kind: SourceKind
    source_relative_path: str
    sheet_name: str | None
    page_number: int | None
    bbox: BoundingBox | None
    source_row_start: int
    source_row_end: int
    source_column_start: int
    source_column_end: int
    row_count: int
    column_count: int
    columns: tuple[str, ...]
    raw_artifact_path: str
    normalized_artifact_path: str | None
    metadata_artifact_path: str
    extraction_confidence: float | None
    quality_status: AssetQualityStatus
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "table_id",
            "file_id",
            "extraction_run_id",
            "extractor",
            "extractor_version",
            "source_relative_path",
            "raw_artifact_path",
            "metadata_artifact_path",
        ):
            _required(getattr(self, name), name)
        _sha256(self.content_sha256)
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(self, "quality_status", AssetQualityStatus(self.quality_status))
        object.__setattr__(self, "columns", tuple(self.columns))
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("page_number must be one-based")
        if self.row_count < 0 or self.column_count < 0:
            raise ValueError("row_count and column_count must be non-negative")
        if min(self.source_row_start, self.source_column_start) < 0:
            raise ValueError("source ranges must be non-negative")
        if self.source_row_end < self.source_row_start or self.source_column_end < self.source_column_start:
            raise ValueError("source ranges must be ordered half-open intervals")
        if len(self.columns) != self.column_count:
            raise ValueError("column_count must match columns")
        if self.normalized_artifact_path is not None:
            _required(self.normalized_artifact_path, "normalized_artifact_path")
        _confidence(self.extraction_confidence)
        _timestamp(self.created_at, "created_at")


@dataclass(frozen=True)
class TextAsset:
    text_asset_id: str
    file_id: str
    content_sha256: str
    extraction_run_id: str
    extractor: str
    extractor_version: str
    source_kind: SourceKind
    page_number: int | None
    section: str | None
    bbox: BoundingBox | None
    text: str
    language: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("text_asset_id", "file_id", "extraction_run_id", "extractor", "extractor_version"):
            _required(getattr(self, name), name)
        _sha256(self.content_sha256)
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("page_number must be one-based")
        _timestamp(self.created_at, "created_at")


@dataclass(frozen=True)
class ChunkProvenance:
    file_id: str
    content_sha256: str
    text_asset_id: str
    extraction_run_id: str
    extractor: str
    extractor_version: str
    source_kind: SourceKind
    page_number: int | None
    section: str | None

    def __post_init__(self) -> None:
        for name in ("file_id", "text_asset_id", "extraction_run_id", "extractor", "extractor_version"):
            _required(getattr(self, name), name)
        _sha256(self.content_sha256)
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("page_number must be one-based")


@dataclass(frozen=True)
class TextChunk:
    chunk_id: str
    text_asset_id: str
    file_id: str
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    provenance: ChunkProvenance

    def __post_init__(self) -> None:
        for name in ("chunk_id", "text_asset_id", "file_id"):
            _required(getattr(self, name), name)
        if self.chunk_index < 0:
            raise ValueError("chunk_index must be non-negative")
        if self.char_start < 0 or self.char_end < self.char_start:
            raise ValueError("chunk character offsets must be ordered and non-negative")
        if self.provenance.file_id != self.file_id or self.provenance.text_asset_id != self.text_asset_id:
            raise ValueError("chunk provenance must identify the same file and text asset")


@dataclass(frozen=True)
class SemanticMetadata:
    asset_id: str
    asset_type: AssetType
    display_name: str
    category: str
    description: str
    keywords: tuple[str, ...]
    summary: str
    semantic_fields: Mapping[str, object]
    model: str
    prompt_version: str
    confidence: float | None
    generated_at: datetime

    def __post_init__(self) -> None:
        for name in ("asset_id", "model", "prompt_version"):
            _required(getattr(self, name), name)
        object.__setattr__(self, "asset_type", AssetType(self.asset_type))
        object.__setattr__(self, "keywords", tuple(self.keywords))
        _confidence(self.confidence)
        _timestamp(self.generated_at, "generated_at")


@dataclass(frozen=True)
class QualityIssue:
    issue_id: str
    asset_id: str
    severity: QualityIssueSeverity
    issue_type: str
    description: str
    evidence: Mapping[str, object]
    detected_by: str
    suggested_action: str
    status: QualityIssueStatus

    def __post_init__(self) -> None:
        for name in ("issue_id", "asset_id", "issue_type", "description", "detected_by", "suggested_action"):
            _required(getattr(self, name), name)
        object.__setattr__(self, "severity", QualityIssueSeverity(self.severity))
        object.__setattr__(self, "status", QualityIssueStatus(self.status))


@dataclass(frozen=True)
class FileAssetSet:
    """The independent 0..N table and 0..N text outputs for one file."""

    file_id: str
    table_assets: tuple[TableAsset, ...] = ()
    text_assets: tuple[TextAsset, ...] = ()

    def __post_init__(self) -> None:
        _required(self.file_id, "file_id")
        object.__setattr__(self, "table_assets", tuple(self.table_assets))
        object.__setattr__(self, "text_assets", tuple(self.text_assets))
        assets = (*self.table_assets, *self.text_assets)
        if any(asset.file_id != self.file_id for asset in assets):
            raise ValueError("every asset must refer to the FileAssetSet file_id")
        identifiers = [asset.table_id for asset in self.table_assets]
        identifiers.extend(asset.text_asset_id for asset in self.text_assets)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("asset IDs must be unique within a file")
