"""Small, offline DOCX extractor built on the OOXML package contract.

DOCX is a ZIP/XML container, so the portable runtime does not need Microsoft
Word or another system service.  This route intentionally publishes one text
asset for the document and one table asset per embedded table while keeping
paragraph/table indexes in artifact metadata for precise provenance.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from uuid import uuid4
import zipfile
import xml.etree.ElementTree as ET

from chongzu import paths

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

from chongzu.assets import (
    AssetQualityStatus,
    ChunkProvenance,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueStatus,
    SourceKind,
    TableAsset,
    TextAsset,
    TextChunk,
    make_chunk_id,
    make_table_id,
    make_text_asset_id,
    utc_now,
)
from chongzu.cancellation import check_cancel
from chongzu.registry import Registry, canonical_source_root, utc_now as registry_now
from chongzu.scan import ScanError, scan_source

from .artifacts import matrix_frame, normalize_column_names, write_json_atomic, write_parquet_atomic, workspace_relative
from .models import StructuredSource
from .pdf.artifacts import write_text_asset
from .pdf.blocks import chunk_text, normalize_text


EXTRACTOR_NAME = "docx-stdlib"
EXTRACTOR_VERSION = "stdlib-ooxml-v1"
MAX_DOCX_WORKERS = 4


@dataclass
class DocxStageTimings:
    package_read_ms: float = 0.0
    xml_parse_ms: float = 0.0
    artifact_write_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "package_read_ms": self.package_read_ms,
            "xml_parse_ms": self.xml_parse_ms,
            "artifact_write_ms": self.artifact_write_ms,
        }


@dataclass
class DocxExtractionResult:
    source: StructuredSource
    extraction_run_id: str
    extraction_identity: str
    status: str = "successful"
    assets: list[TableAsset] = field(default_factory=list)
    text_assets: list[TextAsset] = field(default_factory=list)
    text_chunks: list[TextChunk] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    timings: DocxStageTimings = field(default_factory=DocxStageTimings)
    paragraph_count: int = 0
    table_count: int = 0
    error_category: str | None = None
    error_message: str | None = None

    @property
    def extractor(self) -> str:
        return EXTRACTOR_NAME

    @property
    def extractor_version(self) -> str:
        return EXTRACTOR_VERSION

    @property
    def total_rows(self) -> int:
        return sum(asset.row_count for asset in self.assets)


@dataclass
class DocxExtractionSummary:
    source_root: str
    files_considered: int = 0
    docx_files: int = 0
    extracted: int = 0
    reused: int = 0
    failed: int = 0
    tables_produced: int = 0
    text_assets_produced: int = 0
    text_chunks_produced: int = 0
    quality_issues: int = 0
    wall_time_ms: float = 0.0


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element) -> str:
    pieces: list[str] = []
    for child in element.iter():
        name = _local(child.tag)
        if name == "t":
            pieces.append(child.text or "")
        elif name == "tab":
            pieces.append("\t")
        elif name == "br" or name == "cr":
            pieces.append("\n")
    return "".join(pieces).strip()


def _table_rows(element: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in element.iter():
        if _local(tr.tag) != "tr":
            continue
        cells: list[str] = []
        for tc in tr:
            if _local(tc.tag) != "tc":
                continue
            cells.append(_text(tc))
        if cells:
            rows.append(cells)
    return rows


def _table_presentation(element: ET.Element) -> dict[str, Any]:
    """Recover OOXML occupancy without padding the source into a matrix."""

    def attr(item: ET.Element, name: str) -> str | None:
        return item.get(f"{{{W_NS}}}{name}", item.get(name))

    cells: list[dict[str, Any]] = []
    occupancy: list[list[int | None]] = []
    open_vertical: dict[int, int] = {}
    row_count = 0
    max_column = 0
    grid_widths: list[int] = []
    tbl_grid = next((item for item in element if _local(item.tag) == "tblGrid"), None)
    if tbl_grid is not None:
        for grid_column in [item for item in tbl_grid if _local(item.tag) == "gridCol"]:
            width = attr(grid_column, "w")
            if str(width or "").isdigit():
                grid_widths.append(int(width))

    rows = [item for item in element if _local(item.tag) == "tr"]
    for tr in rows:
        row_number = row_count
        row_cells: list[int | None] = []
        cursor = 0
        previous_origins = set(open_vertical.values())
        continued_origins: set[int] = set()

        def mark(start: int, span: int, cell_index: int) -> None:
            end = start + span
            if len(row_cells) < end:
                row_cells.extend([None] * (end - len(row_cells)))
            for index in range(start, end):
                row_cells[index] = cell_index

        for tc in [item for item in tr if _local(item.tag) == "tc"]:
            properties = next((item for item in tc if _local(item.tag) == "tcPr"), None)
            grid_span = 1
            width: str | None = None
            vertical = ""
            if properties is not None:
                grid = next((item for item in properties if _local(item.tag) == "gridSpan"), None)
                if grid is not None:
                    try:
                        grid_span = max(1, int(attr(grid, "val") or "1"))
                    except (TypeError, ValueError):
                        grid_span = 1
                cell_width = next((item for item in properties if _local(item.tag) == "tcW"), None)
                if cell_width is not None:
                    width = attr(cell_width, "w")
                merge = next((item for item in properties if _local(item.tag) == "vMerge"), None)
                if merge is not None:
                    vertical = str(attr(merge, "val") or "continue")

            if vertical == "continue":
                candidate_columns = [index for index in open_vertical if index >= cursor]
                if candidate_columns:
                    start = min(candidate_columns)
                    cell_index = open_vertical[start]
                    origin = cells[cell_index]
                    span = int(origin["column_span"])
                    origin["row_span"] = int(origin.get("row_span", 1)) + 1
                    mark(start, span, cell_index)
                    continued_origins.add(cell_index)
                    cursor = start + span
                    continue

            while vertical != "restart" and any(index in open_vertical for index in range(cursor, cursor + grid_span)):
                cursor = max(index + 1 for index in range(cursor, cursor + grid_span) if index in open_vertical)
            start = cursor
            grid_width = sum(grid_widths[start : start + grid_span]) if start < len(grid_widths) else None
            cell = {
                "row": row_number,
                "column": start,
                "row_span": 1,
                "column_span": grid_span,
                "text": _text(tc),
                "width_twips": int(width) if str(width or "").isdigit() else None,
                "grid_width_twips": grid_width,
            }
            cells.append(cell)
            cell_index = len(cells) - 1
            mark(start, grid_span, cell_index)
            if vertical == "restart":
                for index in range(start, start + grid_span):
                    open_vertical[index] = cell_index
            cursor = start + grid_span

        for origin in previous_origins - continued_origins:
            for index, cell_index in list(open_vertical.items()):
                if cell_index == origin:
                    del open_vertical[index]
        max_column = max(max_column, len(row_cells))
        occupancy.append(row_cells)
        row_count += 1

    for row in occupancy:
        row.extend([None] * (max_column - len(row)))
    return {
        "format": "docx-ooxml-presentation-v1",
        "row_count": row_count,
        "column_count": max_column,
        "grid_widths_twips": grid_widths,
        "occupancy": occupancy,
        "cells": cells,
        "form_like": any(int(item.get("row_span", 1)) > 1 or int(item.get("column_span", 1)) > 1 for item in cells),
    }


def extraction_identity(source: StructuredSource) -> str:
    payload = {
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "business_format": source.business_format,
        "extractor": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "config_version": paths.DOCX_CONFIG_VERSION,
        "chunk_config_version": paths.TEXT_CHUNK_CONFIG_VERSION,
        "registry_schema_version": paths.EXTRACTION_IDENTITY_SCHEMA_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _issue(source: StructuredSource, asset_id: str, issue_type: str, evidence: dict[str, Any]) -> QualityIssue:
    identity = json.dumps([source.file_id, source.content_sha256, asset_id, issue_type, evidence], sort_keys=True).encode("utf-8")
    return QualityIssue(
        issue_id=f"issue_{hashlib.sha256(identity).hexdigest()[:32]}",
        asset_id=asset_id,
        severity=QualityIssueSeverity.WARNING,
        issue_type=issue_type,
        description=issue_type.replace("_", " "),
        evidence=evidence,
        detected_by=f"{EXTRACTOR_NAME}:{EXTRACTOR_VERSION}",
        suggested_action="Review the DOCX source boundary before downstream processing.",
        status=QualityIssueStatus.OPEN,
    )


def _write_table(
    source: StructuredSource,
    result: DocxExtractionResult,
    rows: list[list[str]],
    table_index: int,
    *,
    document_order: int | None = None,
    presentation: Mapping[str, Any] | None = None,
) -> TableAsset | None:
    if not rows:
        return None
    width = max(len(row) for row in rows)
    padded = [row[:width] + [""] * max(0, width - len(row)) for row in rows]
    form_like = bool((presentation or {}).get("form_like"))
    header = padded[0] if not form_like and len(padded) > 1 and sum(bool(value.strip()) for value in padded[0]) >= 2 else None
    data_rows = padded[1:] if header is not None else padded
    names, _mapping, duplicate = normalize_column_names(header or (), width)
    if header is None:
        names = normalize_column_names((), width)[0]
    table_id = make_table_id(
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extractor=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION,
        source_kind=SourceKind.SECTION,
        source_locator=f"docx:table:{table_index}:config:{paths.DOCX_CONFIG_VERSION}",
        asset_index=table_index,
    )
    target = source.workspace_root / "artifacts" / "tables" / table_id
    raw_path = target / "raw.parquet"
    normalized_path = target / "normalized.parquet"
    metadata_path = target / "metadata.json"
    result.timings.artifact_write_ms += write_parquet_atomic(matrix_frame(padded), raw_path)
    result.timings.artifact_write_ms += write_parquet_atomic(matrix_frame(data_rows, names), normalized_path)
    metadata = {
        "contract_version": paths.DOCX_CONFIG_VERSION,
        "table_id": table_id,
        "file_id": source.file_id,
        "content_sha256": source.content_sha256,
        "source_relative_path": source.relative_path,
        "table_index": table_index,
        "document_order": document_order,
        "source_kind": SourceKind.SECTION.value,
        "source_range": {
            "table_index": table_index,
            "row_start": 0,
            "row_end": len(padded),
            "column_start": 0,
            "column_end": width,
            "coordinate_system": "zero-based half-open",
        },
        "header_detected": header is not None,
        "duplicate_column_names": duplicate,
        "raw_artifact": workspace_relative(raw_path, source.workspace_root),
        "normalized_artifact": workspace_relative(normalized_path, source.workspace_root),
        "extraction_run_id": result.extraction_run_id,
        "extractor": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "presentation": dict(presentation or {}),
    }
    write_json_atomic(metadata_path, metadata)
    return TableAsset(
        table_id=table_id,
        file_id=source.file_id,
        content_sha256=source.content_sha256,
        extraction_run_id=result.extraction_run_id,
        extractor=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION,
        source_kind=SourceKind.SECTION,
        source_relative_path=source.relative_path,
        sheet_name=f"table-{table_index + 1}",
        page_number=None,
        bbox=None,
        source_row_start=0,
        source_row_end=len(padded),
        source_column_start=0,
        source_column_end=width,
        row_count=len(data_rows),
        column_count=width,
        columns=tuple(names),
        raw_artifact_path=workspace_relative(raw_path, source.workspace_root),
        normalized_artifact_path=workspace_relative(normalized_path, source.workspace_root),
        metadata_artifact_path=workspace_relative(metadata_path, source.workspace_root),
        extraction_confidence=None,
        quality_status=AssetQualityStatus.REVIEW if duplicate else AssetQualityStatus.PASS,
        created_at=utc_now(),
    )


def _extract_one(source: StructuredSource, run_id: str, identity: str) -> DocxExtractionResult:
    result = DocxExtractionResult(source=source, extraction_run_id=run_id, extraction_identity=identity)
    try:
        before = source.path.stat()
        if (before.st_size, before.st_mtime_ns) != (source.size_bytes, source.mtime_ns):
            raise ValueError("source size or mtime changed after registry scan")
        started = time.perf_counter_ns()
        with zipfile.ZipFile(source.path, "r") as package:
            try:
                xml = package.read("word/document.xml")
            except KeyError as exc:
                raise ValueError("DOCX package is missing word/document.xml") from exc
        result.timings.package_read_ms = (time.perf_counter_ns() - started) / 1_000_000
        started = time.perf_counter_ns()
        root = ET.fromstring(xml)
        body = next((child for child in root.iter() if _local(child.tag) == "body"), None)
        if body is None:
            raise ValueError("DOCX document body is missing")
        paragraphs: list[dict[str, Any]] = []
        table_rows: list[list[list[str]]] = []
        table_presentations: list[dict[str, Any]] = []
        table_orders: list[int] = []
        paragraph_ordinal = 0
        body_order = 0
        for child in body:
            name = _local(child.tag)
            if name == "p":
                value = _text(child)
                if value:
                    paragraphs.append({"paragraph_index": paragraph_ordinal, "document_order": body_order, "text": value})
                paragraph_ordinal += 1
                body_order += 1
            elif name == "tbl":
                table_rows.append(_table_rows(child))
                table_presentations.append(_table_presentation(child))
                table_orders.append(body_order)
                body_order += 1
        result.timings.xml_parse_ms = (time.perf_counter_ns() - started) / 1_000_000
        result.paragraph_count = len(paragraphs)
        result.table_count = len(table_rows)
        normalized = normalize_text("\n".join(item["text"] for item in paragraphs))
        if paragraphs or normalized:
            text_id = make_text_asset_id(
                file_id=source.file_id,
                content_sha256=source.content_sha256,
                extractor=EXTRACTOR_NAME,
                extractor_version=EXTRACTOR_VERSION,
                source_kind=SourceKind.FILE,
                source_locator=f"docx:paragraphs:config:{paths.DOCX_CONFIG_VERSION}",
                asset_index=0,
            )
            offsets: list[dict[str, Any]] = []
            cursor = 0
            for item in paragraphs:
                value = str(item["text"])
                offsets.append(
                    {
                        "paragraph_index": item["paragraph_index"],
                        "document_order": item.get("document_order"),
                        "char_start": cursor,
                        "char_end": cursor + len(value),
                        "text": value,
                    }
                )
                cursor += len(value) + 1
            metadata = {
                "contract_version": paths.DOCX_CONFIG_VERSION,
                "file_id": source.file_id,
                "content_sha256": source.content_sha256,
                "source_relative_path": source.relative_path,
                "extraction_run_id": run_id,
                "extractor": EXTRACTOR_NAME,
                "extractor_version": EXTRACTOR_VERSION,
                "source_kind": SourceKind.FILE.value,
                "section": "docx-paragraphs",
                "paragraphs": offsets,
                "table_count": len(table_rows),
            }
            started = time.perf_counter_ns()
            paths_written = write_text_asset(
                workspace_root=source.workspace_root,
                text_asset_id=text_id,
                raw_text="\n".join(item["text"] for item in paragraphs),
                normalized_text=normalized,
                metadata=metadata,
            )
            result.timings.artifact_write_ms += (time.perf_counter_ns() - started) / 1_000_000
            result.text_assets.append(TextAsset(
                text_asset_id=text_id,
                file_id=source.file_id,
                content_sha256=source.content_sha256,
                extraction_run_id=run_id,
                extractor=EXTRACTOR_NAME,
                extractor_version=EXTRACTOR_VERSION,
                source_kind=SourceKind.FILE,
                page_number=None,
                section="docx-paragraphs",
                bbox=None,
                text=normalized,
                language=None,
                created_at=utc_now(),
                source_relative_path=source.relative_path,
                raw_artifact_path=paths_written["raw"],
                normalized_artifact_path=paths_written["normalized"],
                metadata_artifact_path=paths_written["metadata"],
            ))
            for chunk in chunk_text(normalized):
                result.text_chunks.append(TextChunk(
                    chunk_id=make_chunk_id(text_asset_id=text_id, chunk_index=chunk.chunk_index, char_start=chunk.char_start, char_end=chunk.char_end, chunk_config_version=paths.TEXT_CHUNK_CONFIG_VERSION),
                    text_asset_id=text_id,
                    file_id=source.file_id,
                    chunk_index=chunk.chunk_index,
                    text=chunk.text,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    provenance=ChunkProvenance(
                        file_id=source.file_id,
                        content_sha256=source.content_sha256,
                        text_asset_id=text_id,
                        extraction_run_id=run_id,
                        extractor=EXTRACTOR_NAME,
                        extractor_version=EXTRACTOR_VERSION,
                        source_kind=SourceKind.FILE,
                        page_number=None,
                        section="docx-paragraphs",
                    ),
                ))
        for index, rows in enumerate(table_rows):
            asset = _write_table(
                source,
                result,
                rows,
                index,
                document_order=table_orders[index] if index < len(table_orders) else None,
                presentation=table_presentations[index] if index < len(table_presentations) else None,
            )
            if asset is None:
                result.warnings.append({"table_index": index, "warning": "empty_embedded_table"})
                continue
            result.assets.append(asset)
        if not result.text_assets and not result.assets:
            result.warnings.append({"warning": "empty_docx"})
        after = source.path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise ValueError("source size or mtime changed during DOCX extraction")
    except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
        result.status = "failed"
        result.assets.clear()
        result.text_assets.clear()
        result.text_chunks.clear()
        result.error_category = "docx_extraction_error"
        result.error_message = str(exc)
    return result


def _source_from_row(row: dict[str, Any], workspace_root: Path) -> StructuredSource:
    return StructuredSource(
        file_id=str(row["file_id"]),
        content_sha256=str(row["sha256"]),
        source_root=str(row["source_root"]),
        relative_path=str(row["relative_path"]),
        business_format="docx",
        size_bytes=int(row["size_bytes"] or 0),
        mtime_ns=int(row["mtime_ns"] or 0),
        workspace_root=workspace_root,
    )


def extract_docx(
    source: Path | str,
    *,
    workers: int | None = None,
    force: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    registry: Registry | None = None,
    _scan_summary=None,
    file_ids: set[str] | None = None,
    _skip_recovery: bool = False,
    progress_callback: Callable[..., None] | None = None,
    cancel_event=None,
) -> DocxExtractionSummary:
    wall_started = time.perf_counter_ns()
    worker_count = max(1, min(MAX_DOCX_WORKERS, int(workers or 1)))
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    if _scan_summary is None:
        try:
            scan_summary = scan_source(source, workers=worker_count, registry_path=registry_file, cancel_event=cancel_event)
        except ScanError as exc:
            raise RuntimeError(str(exc)) from exc
    else:
        scan_summary = _scan_summary
    source_root = canonical_source_root(source, require_directory=True)
    summary = DocxExtractionSummary(source_root=source_root, files_considered=0, wall_time_ms=0.0)
    owns_registry = registry is None
    registry = registry or Registry.open(registry_file, initialize=False)
    try:
        if not _skip_recovery:
            registry.recover_incomplete_extractions(source_root)
        rows = registry.docx_candidates(source_root)
        if file_ids is not None:
            rows = [row for row in rows if str(row.get("file_id") or "") in file_ids]
        summary.files_considered = registry.count_present_files(source_root)
        summary.docx_files = len(rows)
        pending: dict[Future[DocxExtractionResult], tuple[StructuredSource, datetime, str]] = {}
        sources = iter(_source_from_row(row, workspace) for row in rows)
        exhausted = False
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="chongzu-docx")
        try:
            while pending or not exhausted:
                check_cancel(cancel_event)
                while not exhausted and len(pending) < worker_count * 2:
                    check_cancel(cancel_event)
                    try:
                        docx_source = next(sources)
                    except StopIteration:
                        exhausted = True
                        break
                    identity = extraction_identity(docx_source)
                    run_id = f"xrun_{uuid4().hex}"
                    started_at = registry_now()
                    registry.start_docx_extraction(extraction_run_id=run_id, extraction_identity=identity, source=docx_source, started_at=started_at, force=force)
                    if progress_callback is not None:
                        progress_callback("extract", 0.44, current_file=docx_source.relative_path, completed=summary.extracted + summary.failed, total=len(rows), current_substage="DOCX paragraphs and tables")
                    future = executor.submit(_extract_one, docx_source, run_id, identity)
                    pending[future] = (docx_source, started_at, run_id)
                if not pending:
                    continue
                done, _ = wait(tuple(pending), timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    docx_source, started_at, run_id = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = DocxExtractionResult(source=docx_source, extraction_run_id=run_id, extraction_identity=extraction_identity(docx_source), status="failed", error_category="worker_error", error_message=str(exc))
                    registry.record_docx_result(result, started_at=started_at, finished_at=registry_now(), force=force)
                    summary.quality_issues += len(result.issues)
                    if result.status == "failed":
                        summary.failed += 1
                    else:
                        summary.extracted += 1
                        summary.tables_produced += len(result.assets)
                        summary.text_assets_produced += len(result.text_assets)
                        summary.text_chunks_produced += len(result.text_chunks)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    finally:
        if owns_registry:
            registry.close()
    summary.wall_time_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    return summary
