"""Deterministic plain-text extraction used by the formal unified pipeline."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from chongzu import paths
from chongzu.assets import (
    ChunkProvenance,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueStatus,
    SourceKind,
    TextAsset,
    TextChunk,
    make_chunk_id,
    make_text_asset_id,
    utc_now,
)
from chongzu.registry import Registry, canonical_source_root, utc_now as registry_now
from chongzu.scan import ScanError, scan_source

from .models import StructuredSource
from .pdf.artifacts import write_text_asset
from .pdf.blocks import chunk_text, normalize_text


EXTRACTOR_NAME = "plain-text"
EXTRACTOR_VERSION = "stdlib-v1"
MAX_TEXT_WORKERS = 8


class TextExtractionError(RuntimeError):
    pass


@dataclass
class TextStageTimings:
    read_ms: float = 0.0
    artifact_write_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {"read_ms": self.read_ms, "artifact_write_ms": self.artifact_write_ms}


@dataclass
class TextExtractionResult:
    source: StructuredSource
    extraction_run_id: str
    extraction_identity: str
    status: str = "successful"
    text_assets: list[TextAsset] = field(default_factory=list)
    text_chunks: list[TextChunk] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    timings: TextStageTimings = field(default_factory=TextStageTimings)
    error_category: str | None = None
    error_message: str | None = None

    @property
    def extractor(self) -> str:
        return EXTRACTOR_NAME

    @property
    def extractor_version(self) -> str:
        return EXTRACTOR_VERSION

    @property
    def total_chars(self) -> int:
        return sum(len(asset.text) for asset in self.text_assets)


@dataclass
class TextExtractionSummary:
    source_root: str
    files_considered: int = 0
    text_files: int = 0
    extracted: int = 0
    reused: int = 0
    failed: int = 0
    text_assets_produced: int = 0
    text_chunks_produced: int = 0
    text_chars: int = 0
    quality_issues: int = 0
    read_ms: float = 0.0
    artifact_write_ms: float = 0.0
    registry_write_ms: float = 0.0
    discovery_scan_ms: float = 0.0
    wall_time_ms: float = 0.0

    def benchmark_metrics(self) -> dict[str, float | int | str]:
        seconds = self.wall_time_ms / 1000.0 if self.wall_time_ms else 0.0
        return {
            "files": self.text_files,
            "text assets": self.text_assets_produced,
            "text chunks": self.text_chunks_produced,
            "text chars": self.text_chars,
            "files/sec": self.text_files / seconds if seconds else 0.0,
            "read ms": self.read_ms,
            "artifact write ms": self.artifact_write_ms,
            "registry write ms": self.registry_write_ms,
            "wall time ms": self.wall_time_ms,
        }


def normalize_workers(workers: int | None) -> int:
    value = 1 if workers is None else int(workers)
    if value < 1 or value > MAX_TEXT_WORKERS:
        raise ValueError(f"text workers must be between 1 and {MAX_TEXT_WORKERS}")
    return value


def _source_from_row(row: dict[str, object], workspace_root: Path) -> StructuredSource:
    return StructuredSource(
        file_id=str(row["file_id"]),
        content_sha256=str(row["sha256"]),
        source_root=str(row["source_root"]),
        relative_path=str(row["relative_path"]),
        business_format="txt",
        size_bytes=int(row["size_bytes"] or 0),
        mtime_ns=int(row["mtime_ns"] or 0),
        workspace_root=workspace_root,
    )


def extraction_identity(source: StructuredSource) -> str:
    payload = {
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "business_format": source.business_format,
        "extractor": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "config_version": paths.TEXT_CONFIG_VERSION,
        "chunk_config_version": paths.TEXT_CHUNK_CONFIG_VERSION,
        "pipeline_version": paths.TEXT_PIPELINE_VERSION,
        "registry_schema_version": paths.EXTRACTION_IDENTITY_SCHEMA_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _decode_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("chongzu", raw, 0, min(1, len(raw)), "unsupported text encoding")


def _issue(source: StructuredSource, asset_id: str, issue_type: str, evidence: dict[str, Any]) -> QualityIssue:
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
        detected_by=f"{EXTRACTOR_NAME}:{EXTRACTOR_VERSION}",
        suggested_action="Review the source text before downstream semantic processing.",
        status=QualityIssueStatus.OPEN,
    )


def _extract_one(source: StructuredSource, run_id: str, identity: str) -> TextExtractionResult:
    result = TextExtractionResult(source=source, extraction_run_id=run_id, extraction_identity=identity)
    try:
        before = source.path.stat()
        if (before.st_size, before.st_mtime_ns) != (source.size_bytes, source.mtime_ns):
            raise TextExtractionError("source size or mtime changed after registry scan")
        started = time.perf_counter_ns()
        raw_text, encoding = _decode_text(source.path)
        result.timings.read_ms = (time.perf_counter_ns() - started) / 1_000_000
        normalized = normalize_text(raw_text)
        asset_id = make_text_asset_id(
            file_id=source.file_id,
            content_sha256=source.content_sha256,
            extractor=EXTRACTOR_NAME,
            extractor_version=EXTRACTOR_VERSION,
            source_kind=SourceKind.FILE,
            source_locator=f"file:txt:config:{paths.TEXT_CONFIG_VERSION}",
            asset_index=0,
        )
        metadata = {
            "contract_version": paths.TEXT_CONFIG_VERSION,
            "file_id": source.file_id,
            "content_sha256": source.content_sha256,
            "source_relative_path": source.relative_path,
            "extraction_run_id": run_id,
            "extractor": EXTRACTOR_NAME,
            "extractor_version": EXTRACTOR_VERSION,
            "source_kind": SourceKind.FILE.value,
            "encoding": encoding,
            "chunk_config_version": paths.TEXT_CHUNK_CONFIG_VERSION,
            "offset_basis": "normalized_text",
        }
        artifact_started = time.perf_counter_ns()
        paths_written = write_text_asset(
            workspace_root=source.workspace_root,
            text_asset_id=asset_id,
            raw_text=raw_text,
            normalized_text=normalized,
            metadata=metadata,
        )
        result.timings.artifact_write_ms = (time.perf_counter_ns() - artifact_started) / 1_000_000
        asset = TextAsset(
            text_asset_id=asset_id,
            file_id=source.file_id,
            content_sha256=source.content_sha256,
            extraction_run_id=run_id,
            extractor=EXTRACTOR_NAME,
            extractor_version=EXTRACTOR_VERSION,
            source_kind=SourceKind.FILE,
            page_number=None,
            section="text",
            bbox=None,
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
                        text_asset_id=asset_id,
                        chunk_index=chunk.chunk_index,
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                        chunk_config_version=paths.TEXT_CHUNK_CONFIG_VERSION,
                    ),
                    text_asset_id=asset_id,
                    file_id=source.file_id,
                    chunk_index=chunk.chunk_index,
                    text=chunk.text,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    provenance=ChunkProvenance(
                        file_id=source.file_id,
                        content_sha256=source.content_sha256,
                        text_asset_id=asset_id,
                        extraction_run_id=run_id,
                        extractor=EXTRACTOR_NAME,
                        extractor_version=EXTRACTOR_VERSION,
                        source_kind=SourceKind.FILE,
                        page_number=None,
                        section="text",
                    ),
                )
            )
        if not normalized:
            result.issues.append(_issue(source, asset_id, "empty_text", {"source_relative_path": source.relative_path}))
        after = source.path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise TextExtractionError("source size or mtime changed during text extraction")
    except Exception as exc:  # isolate one text file
        result.status = "failed"
        result.text_assets.clear()
        result.text_chunks.clear()
        result.issues.clear()
        result.error_category = "text_extraction_error"
        result.error_message = str(exc)
    return result


def _artifacts_exist(reusable: dict[str, Any], workspace_root: Path) -> bool:
    artifacts = reusable.get("artifacts") or []
    if not artifacts:
        return False
    try:
        return all((workspace_root / str(path)).resolve().is_file() for path in artifacts)
    except OSError:
        return False


def extract_text(
    source: Path | str,
    *,
    workers: int | None = None,
    force: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    _scan_summary=None,
) -> TextExtractionSummary:
    """Extract current TXT registry rows into the normal TextAsset contract."""

    wall_started = time.perf_counter_ns()
    worker_count = normalize_workers(workers)
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if _scan_summary is None:
        try:
            scan_summary = scan_source(source, workers=worker_count, registry_path=registry_file)
        except ScanError as exc:
            raise TextExtractionError(str(exc)) from exc
    else:
        scan_summary = _scan_summary
    source_root = canonical_source_root(source, require_directory=True)
    summary = TextExtractionSummary(source_root=source_root, discovery_scan_ms=scan_summary.elapsed_ms)
    registry = Registry.open(registry_file)
    try:
        registry.recover_incomplete_extractions(source_root)
        rows = registry.text_candidates(source_root)
        summary.files_considered = registry.count_present_files(source_root)
        summary.text_files = len(rows)
        pending: dict[Future[TextExtractionResult], tuple[StructuredSource, datetime, bool, str]] = {}
        sources = iter(_source_from_row(row, workspace) for row in rows)
        exhausted = False
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="chongzu-text") as executor:
            while pending or not exhausted:
                while not exhausted and len(pending) < worker_count * 2:
                    try:
                        text_source = next(sources)
                    except StopIteration:
                        exhausted = True
                        break
                    identity = extraction_identity(text_source)
                    reusable = None if force else registry.reusable_text_extraction(identity)
                    if reusable is not None and _artifacts_exist(reusable, workspace):
                        summary.reused += 1
                        summary.text_assets_produced += int(reusable.get("text_asset_count") or 0)
                        summary.text_chunks_produced += int(reusable.get("text_chunk_count") or 0)
                        continue
                    run_id = f"xrun_{uuid4().hex}"
                    started_at = registry_now()
                    registry.start_text_extraction(
                        extraction_run_id=run_id,
                        extraction_identity=identity,
                        source=text_source,
                        extractor=EXTRACTOR_NAME,
                        extractor_version=EXTRACTOR_VERSION,
                        started_at=started_at,
                        force=force,
                    )
                    future = executor.submit(_extract_one, text_source, run_id, identity)
                    pending[future] = (text_source, started_at, force, run_id)
                if not pending:
                    continue
                done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in done:
                    text_source, started_at, was_forced, _run_id = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = TextExtractionResult(
                            source=text_source,
                            extraction_run_id=_run_id,
                            extraction_identity=extraction_identity(text_source),
                            status="failed",
                            error_category="worker_error",
                            error_message=str(exc),
                        )
                    registry_started = time.perf_counter_ns()
                    registry.record_text_result(
                        result,
                        started_at=started_at,
                        finished_at=registry_now(),
                        force=was_forced,
                    )
                    summary.registry_write_ms += (time.perf_counter_ns() - registry_started) / 1_000_000
                    summary.read_ms += result.timings.read_ms
                    summary.artifact_write_ms += result.timings.artifact_write_ms
                    summary.quality_issues += len(result.issues)
                    if result.status == "failed":
                        summary.failed += 1
                    else:
                        summary.extracted += 1
                        summary.text_assets_produced += len(result.text_assets)
                        summary.text_chunks_produced += len(result.text_chunks)
                        summary.text_chars += result.total_chars
    finally:
        registry.close()
    summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    return summary
