"""Explicit JPG/PNG Vision extraction coordinator.

This route is intentionally independent from local OCR.  The unified
coordinator selects it only when the caller explicitly requests ``ai_vision``
and supplies a verified provider snapshot.  The resulting assets use the
existing registry/artifact contracts and still pass through deterministic
cleaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from threading import Event
from typing import Any, Callable
from uuid import uuid4

from chongzu import paths
from chongzu.assets import (
    AssetQualityStatus,
    BoundingBox,
    ChunkProvenance,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueStatus,
    SourceKind,
    TableAsset,
    TextAsset,
    TextChunk,
    make_chunk_id,
    make_text_asset_id,
    make_table_id,
    utc_now,
)
from chongzu.cancellation import CancellationRequested, check_cancel
from chongzu.extract.artifacts import (
    artifact_absolute,
    matrix_frame,
    normalize_column_names,
    workspace_relative,
    write_json_atomic,
    write_parquet_atomic,
)
from chongzu.extract.models import StructuredSource
from chongzu.extract.pdf.artifacts import write_text_asset
from chongzu.extract.pdf.blocks import chunk_text, normalize_text
from chongzu.registry import Registry, canonical_source_root, utc_now as registry_now
from chongzu.scan import ScanError, scan_source

from .contract import (
    VISION_CONFIG_VERSION,
    VISION_CONTRACT_VERSION,
    VISION_EXTRACTOR,
    VISION_EXTRACTOR_VERSION,
    VISION_OUTPUT_CONTRACT,
    VISION_PIPELINE_VERSION,
)
from .models import VisionRequest
from .provider import VisionProvider, VisionProviderError
from .validator import VisionContractError, VisionDocument


VISION_MODES = frozenset({"local", "ai_vision"})
MAX_VISION_IMAGE_BYTES = 8 * 1024 * 1024


class VisionExtractionError(RuntimeError):
    """Raised when the Vision coordinator cannot prepare its source."""


@dataclass
class VisionStageTimings:
    image_read_ms: float = 0.0
    provider_ms: float = 0.0
    validation_ms: float = 0.0
    artifact_write_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "image_read_ms": self.image_read_ms,
            "provider_ms": self.provider_ms,
            "validation_ms": self.validation_ms,
            "artifact_write_ms": self.artifact_write_ms,
        }


@dataclass
class VisionExtractionResult:
    source: StructuredSource
    extraction_run_id: str
    extraction_identity: str
    route_reason: str
    pipeline_version: str = VISION_PIPELINE_VERSION
    configuration_version: str = VISION_CONFIG_VERSION
    pages_attempted: int = 0
    pages_succeeded: int = 0
    pages_failed: int = 0
    status: str = "successful"
    text_assets: list[TextAsset] = field(default_factory=list)
    text_chunks: list[TextChunk] = field(default_factory=list)
    table_assets: list[TableAsset] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    timings: VisionStageTimings = field(default_factory=VisionStageTimings)
    error_category: str | None = None
    error_message: str | None = None

    @property
    def extractor(self) -> str:
        return VISION_EXTRACTOR

    @property
    def extractor_version(self) -> str:
        return VISION_EXTRACTOR_VERSION

    @property
    def total_chars(self) -> int:
        return sum(len(asset.text) for asset in self.text_assets)

    @property
    def total_rows(self) -> int:
        return sum(asset.row_count for asset in self.table_assets)


@dataclass
class VisionExtractionSummary:
    source_root: str
    files_considered: int = 0
    files_attempted: int = 0
    extracted: int = 0
    reused: int = 0
    failed: int = 0
    text_assets_produced: int = 0
    table_assets_produced: int = 0
    text_chars: int = 0
    table_rows: int = 0
    provider_calls: int = 0
    quality_issues: int = 0
    image_read_ms: float = 0.0
    provider_ms: float = 0.0
    validation_ms: float = 0.0
    artifact_write_ms: float = 0.0
    registry_write_ms: float = 0.0
    discovery_scan_ms: float = 0.0
    wall_time_ms: float = 0.0
    pages_considered: int = 0
    pages_attempted: int = 0
    pages_succeeded: int = 0
    pages_failed: int = 0
    pdfs_considered: int = 0
    pdfs_attempted: int = 0

    def benchmark_metrics(self) -> dict[str, float | int | str]:
        seconds = self.wall_time_ms / 1000.0 if self.wall_time_ms else 0.0
        return {
            "files": self.files_attempted,
            "extracted": self.extracted,
            "reused": self.reused,
            "failed": self.failed,
            "text assets": self.text_assets_produced,
            "table assets": self.table_assets_produced,
            "text chars": self.text_chars,
            "table rows": self.table_rows,
            "provider calls": self.provider_calls,
            "files/sec": self.files_attempted / seconds if seconds else 0.0,
            "image read ms": self.image_read_ms,
            "provider ms": self.provider_ms,
            "validation ms": self.validation_ms,
            "artifact write ms": self.artifact_write_ms,
            "registry write ms": self.registry_write_ms,
            "wall time ms": self.wall_time_ms,
            "pages": self.pages_attempted,
            "pages succeeded": self.pages_succeeded,
            "pages failed": self.pages_failed,
        }


def normalize_vision_mode(value: object) -> str:
    if value is None or value == "":
        return "local"
    if not isinstance(value, str) or value not in VISION_MODES:
        raise ValueError("vision_mode must be local or ai_vision")
    return value


def _source_from_row(row: dict[str, Any], workspace_root: Path) -> StructuredSource:
    return StructuredSource(
        file_id=str(row["file_id"]),
        content_sha256=str(row["sha256"]),
        source_root=str(row["source_root"]),
        relative_path=str(row["relative_path"]),
        business_format=str(row["business_format"]),
        size_bytes=int(row["size_bytes"] or 0),
        mtime_ns=int(row["mtime_ns"] or 0),
        workspace_root=workspace_root,
    )


def _provider_value(provider: VisionProvider, name: str, default: str) -> str:
    value = getattr(provider, name, default)
    return value if isinstance(value, str) and value else default


def _safe_provider_error(provider: VisionProvider, value: object) -> str:
    message = str(value).strip()
    config = getattr(provider, "config", None)
    secret = getattr(config, "api_key", "") if config is not None else ""
    if isinstance(secret, str) and secret:
        message = message.replace(secret, "[REDACTED]")
    return message[:4_000]


def vision_extraction_identity(source: StructuredSource, provider: VisionProvider) -> str:
    capabilities = getattr(provider, "capabilities", None)
    contract = _provider_value(capabilities, "contract", VISION_CONTRACT_VERSION) if capabilities else VISION_CONTRACT_VERSION
    endpoint = _provider_value(provider, "endpoint", "provider")
    payload = {
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "business_format": source.business_format,
        "extractor": VISION_EXTRACTOR,
        "extractor_version": VISION_EXTRACTOR_VERSION,
        "pipeline_version": VISION_PIPELINE_VERSION,
        "config_version": VISION_CONFIG_VERSION,
        "provider_contract": contract,
        "provider_endpoint": endpoint,
        "model": _provider_value(provider, "model", "unknown"),
        "registry_schema_version": paths.EXTRACTION_IDENTITY_SCHEMA_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _quality_issue(source: StructuredSource, asset_id: str, issue_type: str, evidence: dict[str, Any]) -> QualityIssue:
    identity = json.dumps(
        [source.file_id, source.content_sha256, asset_id, issue_type, evidence],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return QualityIssue(
        issue_id=f"issue_{hashlib.sha256(identity).hexdigest()[:32]}",
        asset_id=asset_id,
        severity=QualityIssueSeverity.WARNING,
        issue_type=issue_type,
        description=issue_type.replace("_", " "),
        evidence=evidence,
        detected_by=f"{VISION_EXTRACTOR}:{VISION_EXTRACTOR_VERSION}",
        suggested_action="Review the Vision output against the original image before semantic processing.",
        status=QualityIssueStatus.OPEN,
    )


def _image_identity(source: StructuredSource, data: bytes, media_type: str) -> dict[str, Any]:
    return {
        "source_relative_path": source.relative_path,
        "content_sha256": source.content_sha256,
        "image_sha256": hashlib.sha256(data).hexdigest(),
        "media_type": media_type,
        "size_bytes": len(data),
    }


def _write_table_asset(
    *,
    source: StructuredSource,
    result: VisionExtractionResult,
    document: VisionDocument,
    table: Any,
    table_index: int,
    image_identity: dict[str, Any],
    provider_name: str,
    provider_model: str,
    provider_contract: str,
    source_kind: SourceKind = SourceKind.IMAGE,
    page_number: int | None = None,
    bbox: BoundingBox | None = None,
    source_locator: str | None = None,
    asset_index: int | None = None,
    render_metadata: dict[str, Any] | None = None,
    pipeline_version: str = VISION_PIPELINE_VERSION,
    configuration_version: str = VISION_CONFIG_VERSION,
) -> None:
    width = len(table.columns)
    locator = source_locator or f"image:vision:table:{table_index}:contract:{VISION_CONTRACT_VERSION}"
    table_id = make_table_id(
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extractor=VISION_EXTRACTOR,
        extractor_version=VISION_EXTRACTOR_VERSION,
        source_kind=source_kind,
        source_locator=locator,
        asset_index=table_index if asset_index is None else asset_index,
    )
    target_dir = source.workspace_root / "artifacts" / "tables" / table_id
    raw_path = target_dir / "raw.parquet"
    normalized_path = target_dir / "normalized.parquet"
    metadata_path = target_dir / "metadata.json"
    raw_names = [f"source_col_{index + 1:04d}" for index in range(width)]
    normalized_names, column_mapping, duplicate = normalize_column_names(table.columns, width)
    raw_frame = matrix_frame(table.rows, raw_names)
    normalized_frame = matrix_frame(table.rows, normalized_names)
    result.timings.artifact_write_ms += write_parquet_atomic(raw_frame, raw_path)
    result.timings.artifact_write_ms += write_parquet_atomic(normalized_frame, normalized_path)
    quality_warnings = ["duplicate_column_names"] if duplicate else []
    if not table.rows:
        quality_warnings.append("empty_table")
    if any(not str(column).strip() for column in table.columns):
        quality_warnings.append("empty_column_header")
    metadata = {
        "contract_version": VISION_CONTRACT_VERSION,
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "source_relative_path": source.relative_path,
        "extraction_run_id": result.extraction_run_id,
        "extractor": VISION_EXTRACTOR,
        "extractor_version": VISION_EXTRACTOR_VERSION,
        "source_kind": source_kind.value,
        "page_number": page_number,
        "source_locator": locator,
        "pipeline_version": pipeline_version,
        "configuration_version": configuration_version,
        "source_range": {
            "row_start": 0,
            "row_end": len(table.rows),
            "column_start": 0,
            "column_end": width,
            "coordinate_system": "zero-based half-open model table coordinates",
        },
        "bbox": bbox.__dict__ if bbox else None,
        "image_identity": image_identity,
        "render_metadata": render_metadata,
        "provider": provider_name,
        "provider_contract": provider_contract,
        "model": provider_model,
        "page_type": document.page_type,
        "document_title": document.title,
        "table_title": table.title,
        "confidence": table.confidence if table.confidence is not None else document.confidence,
        "review_required": True,
        "model_columns": list(table.columns),
        "raw_columns": raw_names,
        "normalized_columns": normalized_names,
        "column_mapping": column_mapping,
        "quality_warnings": quality_warnings,
        "layers": {"raw": "raw.parquet", "normalized": "normalized.parquet", "semantic": None},
    }
    write_json_atomic(metadata_path, metadata)
    asset = TableAsset(
        table_id=table_id,
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extraction_run_id=result.extraction_run_id,
        extractor=VISION_EXTRACTOR,
        extractor_version=VISION_EXTRACTOR_VERSION,
        source_kind=source_kind,
        source_relative_path=source.relative_path,
        sheet_name=None,
        page_number=page_number,
        bbox=bbox,
        source_row_start=0,
        source_row_end=len(table.rows),
        source_column_start=0,
        source_column_end=width,
        row_count=len(table.rows),
        column_count=width,
        columns=tuple(normalized_names),
        raw_artifact_path=workspace_relative(raw_path, source.workspace_root),
        normalized_artifact_path=workspace_relative(normalized_path, source.workspace_root),
        metadata_artifact_path=workspace_relative(metadata_path, source.workspace_root),
        extraction_confidence=table.confidence if table.confidence is not None else document.confidence,
        quality_status=AssetQualityStatus.REVIEW,
        created_at=utc_now(),
    )
    result.table_assets.append(asset)
    if duplicate:
        result.issues.append(
            _quality_issue(
                source,
                table_id,
                "duplicate_column_names",
                {
                    "column_mapping": column_mapping,
                    "source_relative_path": source.relative_path,
                    "page_number": page_number,
                },
            )
        )


def _write_text_asset(
    *,
    source: StructuredSource,
    result: VisionExtractionResult,
    document: VisionDocument,
    image_identity: dict[str, Any],
    provider_name: str,
    provider_model: str,
    provider_contract: str,
    source_kind: SourceKind = SourceKind.IMAGE,
    page_number: int | None = None,
    bbox: BoundingBox | None = None,
    source_locator: str | None = None,
    asset_index: int = 0,
    section: str | None = None,
    render_metadata: dict[str, Any] | None = None,
    pipeline_version: str = VISION_PIPELINE_VERSION,
    configuration_version: str = VISION_CONFIG_VERSION,
) -> None:
    segments: list[str] = []
    if document.title.strip():
        segments.append(document.title.strip())
    segments.extend(item.text for item in document.useful_text if item.text.strip())
    raw_text = "\n".join(segments)
    normalized = normalize_text(raw_text)
    if not normalized.strip():
        return
    locator = source_locator or f"image:vision:text:contract:{VISION_CONTRACT_VERSION}"
    text_asset_id = make_text_asset_id(
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extractor=VISION_EXTRACTOR,
        extractor_version=VISION_EXTRACTOR_VERSION,
        source_kind=source_kind,
        source_locator=locator,
        asset_index=asset_index,
    )
    metadata = {
        "contract_version": VISION_CONTRACT_VERSION,
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "source_relative_path": source.relative_path,
        "extraction_run_id": result.extraction_run_id,
        "extractor": VISION_EXTRACTOR,
        "extractor_version": VISION_EXTRACTOR_VERSION,
        "source_kind": source_kind.value,
        "page_number": page_number,
        "source_locator": locator,
        "pipeline_version": pipeline_version,
        "configuration_version": configuration_version,
        "image_identity": image_identity,
        "render_metadata": render_metadata,
        "provider": provider_name,
        "provider_contract": provider_contract,
        "model": provider_model,
        "page_type": document.page_type,
        "title": document.title,
        "useful_text": [{"text": item.text, "role": item.role} for item in document.useful_text],
        "confidence": document.confidence,
        "chunk_config_version": paths.TEXT_CHUNK_CONFIG_VERSION,
        "offset_basis": "normalized_text",
    }
    artifact_started = time.perf_counter_ns()
    paths_written = write_text_asset(
        workspace_root=source.workspace_root,
        text_asset_id=text_asset_id,
        raw_text=raw_text,
        normalized_text=normalized,
        metadata=metadata,
    )
    result.timings.artifact_write_ms += (time.perf_counter_ns() - artifact_started) / 1_000_000
    asset = TextAsset(
        text_asset_id=text_asset_id,
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extraction_run_id=result.extraction_run_id,
        extractor=VISION_EXTRACTOR,
        extractor_version=VISION_EXTRACTOR_VERSION,
        source_kind=source_kind,
        page_number=page_number,
        section=section or ("vision-image" if page_number is None else f"vision-pdf-page-{page_number}"),
        bbox=bbox,
        text=normalized,
        language=None,
        created_at=utc_now(),
        source_relative_path=source.relative_path,
        raw_artifact_path=paths_written["raw"],
        normalized_artifact_path=paths_written["normalized"],
        metadata_artifact_path=paths_written["metadata"],
    )
    result.text_assets.append(asset)
    for chunk in chunk_text(normalized):
        result.text_chunks.append(
            TextChunk(
                chunk_id=make_chunk_id(
                    text_asset_id=text_asset_id,
                    chunk_index=chunk.chunk_index,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    chunk_config_version=paths.TEXT_CHUNK_CONFIG_VERSION,
                ),
                text_asset_id=text_asset_id,
                file_id=source.file_id,
                chunk_index=chunk.chunk_index,
                text=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                provenance=ChunkProvenance(
                    file_id=source.file_id,
                    content_sha256=source.content_sha256,
                    text_asset_id=text_asset_id,
                    extraction_run_id=result.extraction_run_id,
                    extractor=VISION_EXTRACTOR,
                    extractor_version=VISION_EXTRACTOR_VERSION,
                    source_kind=source_kind,
                    page_number=page_number,
                    section=asset.section,
                ),
            )
        )


def _extract_one(
    source: StructuredSource,
    run_id: str,
    identity: str,
    provider: VisionProvider,
    route_reason: str,
    cancel_event: Event | None = None,
) -> VisionExtractionResult:
    result = VisionExtractionResult(
        source=source,
        extraction_run_id=run_id,
        extraction_identity=identity,
        route_reason=route_reason,
    )
    try:
        check_cancel(cancel_event)
        before = source.path.stat()
        if (before.st_size, before.st_mtime_ns) != (source.size_bytes, source.mtime_ns):
            raise VisionExtractionError("source size or mtime changed after registry scan")
        if before.st_size > MAX_VISION_IMAGE_BYTES:
            raise VisionExtractionError("image exceeds the local Vision size limit")
        image_started = time.perf_counter_ns()
        image_bytes = source.path.read_bytes()
        result.timings.image_read_ms = (time.perf_counter_ns() - image_started) / 1_000_000
        media_type = "image/jpeg" if source.business_format == "jpeg" else "image/png"
        image_identity = _image_identity(source, image_bytes, media_type)
        capabilities = getattr(provider, "capabilities", None)
        if capabilities is None or not bool(getattr(capabilities, "vision", False)):
            raise VisionExtractionError("configured provider does not declare Vision image capability")
        provider_name = _provider_value(provider, "name", "vision-provider")
        provider_model = _provider_value(provider, "model", "unknown")
        provider_contract = _provider_value(capabilities, "contract", VISION_CONTRACT_VERSION)
        request = VisionRequest(
            image_bytes=image_bytes,
            media_type=media_type,
            model=provider_model,
            output_contract=VISION_OUTPUT_CONTRACT,
        )
        provider_started = time.perf_counter_ns()
        response = provider.extract(request)
        result.timings.provider_ms = (time.perf_counter_ns() - provider_started) / 1_000_000
        result.warnings.append(
            {
                "provider": provider_name,
                "provider_contract": provider_contract,
                "model": provider_model,
                "provider_request_id": response.request_id,
                "response_bytes": response.raw_size_bytes,
            }
        )
        check_cancel(cancel_event)
        validation_started = time.perf_counter_ns()
        from .validator import validate_vision_payload

        document = validate_vision_payload(response.payload)
        result.timings.validation_ms = (time.perf_counter_ns() - validation_started) / 1_000_000
        if document.is_empty:
            result.warnings.append({"route_reason": "vision_empty_result", "empty_result": True})
        else:
            for table_index, table in enumerate(document.tables):
                _write_table_asset(
                    source=source,
                    result=result,
                    document=document,
                    table=table,
                    table_index=table_index,
                    image_identity=image_identity,
                    provider_name=provider_name,
                    provider_model=provider_model,
                    provider_contract=provider_contract,
                )
            _write_text_asset(
                source=source,
                result=result,
                document=document,
                image_identity=image_identity,
                provider_name=provider_name,
                provider_model=provider_model,
                provider_contract=provider_contract,
            )
        after = source.path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise VisionExtractionError("source size or mtime changed during Vision extraction")
    except CancellationRequested:
        raise
    except VisionContractError as exc:
        result.status = "failed"
        result.error_category = "vision_contract_error"
        result.error_message = _safe_provider_error(provider, exc)
        result.text_assets.clear()
        result.text_chunks.clear()
        result.table_assets.clear()
        result.issues.clear()
    except VisionProviderError as exc:
        result.status = "failed"
        result.error_category = f"vision_provider_{exc.code}"
        result.error_message = _safe_provider_error(provider, exc)
        result.text_assets.clear()
        result.text_chunks.clear()
        result.table_assets.clear()
        result.issues.clear()
    except Exception as exc:  # one image must not poison the directory
        result.status = "failed"
        result.error_category = "vision_extraction_error"
        result.error_message = _safe_provider_error(provider, exc)
        result.text_assets.clear()
        result.text_chunks.clear()
        result.table_assets.clear()
        result.issues.clear()
    return result


def _artifacts_exist(reusable: dict[str, Any], workspace: Path) -> bool:
    artifacts = [str(item) for item in reusable.get("artifacts", []) if item]
    try:
        return bool(artifacts) and all(artifact_absolute(item, workspace).is_file() for item in artifacts)
    except (OSError, ValueError):
        return False


def _record(
    registry: Registry,
    summary: VisionExtractionSummary,
    result: VisionExtractionResult,
    started_at: datetime,
    force: bool,
) -> None:
    started = time.perf_counter_ns()
    registry.record_vision_result(result, started_at=started_at, finished_at=registry_now(), force=force)
    summary.registry_write_ms += (time.perf_counter_ns() - started) / 1_000_000
    summary.image_read_ms += result.timings.image_read_ms
    summary.provider_ms += result.timings.provider_ms
    summary.validation_ms += result.timings.validation_ms
    summary.artifact_write_ms += result.timings.artifact_write_ms
    summary.quality_issues += len(result.issues)
    summary.text_assets_produced += len(result.text_assets)
    summary.table_assets_produced += len(result.table_assets)
    summary.text_chars += result.total_chars
    summary.table_rows += result.total_rows
    if result.status == "failed":
        summary.failed += 1
    else:
        summary.extracted += 1


def extract_vision(
    source: Path | str,
    *,
    provider: VisionProvider,
    workers: int | None = None,
    force: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    _scan_summary: Any | None = None,
    file_ids: set[str] | None = None,
    _skip_recovery: bool = False,
    progress_callback: Callable[..., None] | None = None,
    cancel_event: Event | None = None,
) -> VisionExtractionSummary:
    """Extract explicitly selected JPG/PNG files through one provider."""

    del workers  # Vision calls are deliberately bounded to one active route.
    wall_started = time.perf_counter_ns()
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if _scan_summary is None:
        try:
            scan_summary = scan_source(source, workers=1, registry_path=registry_file, cancel_event=cancel_event)
        except ScanError as exc:
            raise VisionExtractionError(str(exc)) from exc
    else:
        scan_summary = _scan_summary
    source_root = canonical_source_root(source, require_directory=True)
    summary = VisionExtractionSummary(source_root=source_root, discovery_scan_ms=scan_summary.elapsed_ms)
    if not bool(getattr(getattr(provider, "capabilities", None), "vision", False)):
        raise VisionExtractionError("configured provider does not declare Vision image capability")
    registry = Registry.open(registry_file, initialize=False)
    try:
        if not _skip_recovery:
            registry.recover_incomplete_extractions(source_root)
        rows = registry.vision_candidates(source_root)
        if file_ids is not None:
            rows = [row for row in rows if str(row.get("file_id") or "") in file_ids]
        summary.files_considered = len(rows)
        summary.files_attempted = len(rows)
        total = len(rows)
        completed = 0

        def emit(file_name: str | None, substage: str) -> None:
            if progress_callback is None:
                return
            progress = 0.58 + (0.18 * completed / max(1, total))
            try:
                progress_callback(
                    "vision_extraction",
                    progress,
                    current_file=file_name,
                    completed=completed,
                    total=total,
                    current_substage=substage,
                )
            except TypeError:
                progress_callback("vision_extraction", progress)

        for row in rows:
            check_cancel(cancel_event)
            source_item = _source_from_row(row, workspace)
            emit(source_item.relative_path, "preparing_image")
            identity = vision_extraction_identity(source_item, provider)
            reusable = None if force else registry.reusable_vision_extraction(identity)
            if reusable is not None and _artifacts_exist(reusable, workspace):
                summary.reused += 1
                completed += 1
                emit(source_item.relative_path, "reused_result")
                continue
            run_id = f"vrun_{uuid4().hex}"
            started_at = registry_now()
            registry.start_vision_extraction(
                extraction_run_id=run_id,
                extraction_identity=identity,
                source=source_item,
                extractor=VISION_EXTRACTOR,
                extractor_version=VISION_EXTRACTOR_VERSION,
                started_at=started_at,
                force=force,
                route_reason="explicit_ai_vision_image",
                provider_contract=_provider_value(
                    getattr(provider, "capabilities", None), "contract", VISION_CONTRACT_VERSION
                ),
                provider_model=_provider_value(provider, "model", "unknown"),
            )
            emit(source_item.relative_path, "calling_model")
            result = _extract_one(
                source_item,
                run_id,
                identity,
                provider,
                "explicit_ai_vision_image",
                cancel_event,
            )
            emit(source_item.relative_path, "validating_response")
            _record(registry, summary, result, started_at, force)
            completed += 1
            emit(source_item.relative_path, "writing_assets")
    finally:
        registry.close()
    summary.provider_calls = int(getattr(provider, "call_count", summary.files_attempted - summary.reused))
    summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    return summary
