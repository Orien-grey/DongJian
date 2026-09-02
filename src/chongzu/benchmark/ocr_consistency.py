"""Rendered-page consistency checks for the Phase 5B image route.

The Phase 4C review directory contains PNG renders of native-text PDF pages.
This benchmark treats those renders as simulated scan/screenshot inputs.  It
compares the native candidate's recorded output with the image route for a
small deterministic sample, but never calls the native result ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from chongzu import paths
from chongzu.extract.ocr.runner import OCRExtractionSummary, extract_ocr
from chongzu.extract.pdf.artifacts import write_json_atomic
from chongzu.extract.pdf.table_quality import normalize_cell
from chongzu.registry import Registry, canonical_source_root


CONSISTENCY_VERSION = "pdf-rendered-page-consistency-v1"
_PAGE_NAME = re.compile(r"^page-(\d+)\.png$", re.IGNORECASE)


@dataclass(frozen=True)
class RenderedPageCase:
    sample_id: str
    page_number: int
    relative_path: str
    image_path: Path
    native_tables: tuple[dict[str, Any], ...]

    @property
    def has_native_candidate(self) -> bool:
        return bool(self.native_tables)


@dataclass(frozen=True)
class RenderedPageConsistencyResult:
    report_path: Path
    selected_pages: int
    native_candidate_pages: int
    native_negative_pages: int
    ocr_summary: OCRExtractionSummary
    source_unchanged: bool
    table_count_consistent_pages: int
    shape_consistent_tables: int
    normalized_cell_overlap_cells: int
    normalized_cell_overlap_ratio: float
    first_run_metrics: dict[str, Any]
    reuse_run_metrics: dict[str, Any]


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_number(path: Path) -> int | None:
    match = _PAGE_NAME.match(path.name)
    return int(match.group(1)) if match else None


def select_rendered_pages(review_root: Path | str, *, max_pages: int = 12) -> list[RenderedPageCase]:
    """Select a deterministic half-positive/half-negative rendered sample."""

    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    root = Path(review_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"review root does not exist: {root}")
    positives: list[RenderedPageCase] = []
    negatives: list[RenderedPageCase] = []
    for sample_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        tables_path = sample_dir / "detected_tables.json"
        pages_dir = sample_dir / "pages"
        if not tables_path.is_file() or not pages_dir.is_dir():
            continue
        payload = _json(tables_path)
        by_page: dict[int, list[dict[str, Any]]] = {}
        for table in payload.get("tables", []):
            if not isinstance(table, dict):
                continue
            page_number = int(table.get("page_number") or 0)
            if page_number > 0:
                by_page.setdefault(page_number, []).append(table)
        for image_path in sorted(pages_dir.glob("*.png")):
            page_number = _page_number(image_path)
            if page_number is None:
                continue
            relative_path = image_path.relative_to(root).as_posix()
            case = RenderedPageCase(
                sample_id=sample_dir.name,
                page_number=page_number,
                relative_path=relative_path,
                image_path=image_path,
                native_tables=tuple(by_page.get(page_number, [])),
            )
            (positives if case.has_native_candidate else negatives).append(case)
    positive_limit = min(len(positives), max_pages // 2)
    negative_limit = min(len(negatives), max_pages - positive_limit)
    selected = positives[:positive_limit] + negatives[:negative_limit]
    if len(selected) < max_pages:
        remaining = [case for case in positives[positive_limit:] + negatives[negative_limit:]]
        selected.extend(remaining[: max_pages - len(selected)])
    return selected


def _matrix(path: Path) -> tuple[tuple[Any, ...], ...] | None:
    if not path.is_file():
        return None
    try:
        import polars as pl

        frame = pl.read_parquet(path)
        return tuple(tuple(row) for row in frame.rows())
    except Exception:
        return None


def _native_matrices(case: RenderedPageCase, workspace_root: Path) -> list[tuple[tuple[Any, ...], ...]]:
    matrices: list[tuple[tuple[Any, ...], ...]] = []
    for table in case.native_tables:
        relative = table.get("normalized_artifact_path")
        if not relative:
            continue
        matrix = _matrix(workspace_root / str(relative))
        if matrix is not None:
            matrices.append(matrix)
    return matrices


def _image_tables(
    registry: Registry,
    source_root: str,
    relative_path: str,
    workspace_root: Path,
) -> list[dict[str, Any]]:
    cursor = registry.connection.execute(
        """
        SELECT table_id, row_count, column_count, normalized_artifact_path,
               metadata_artifact_path
        FROM table_assets t
        JOIN files f ON f.file_id=t.file_id
        WHERE f.source_root=? AND t.source_relative_path=?
          AND t.extractor='img2table-image' AND t.is_current=TRUE
        ORDER BY table_id
        """,
        [source_root, relative_path],
    )
    result: list[dict[str, Any]] = []
    for table_id, row_count, column_count, normalized_path, metadata_path in cursor.fetchall():
        metadata = _json(workspace_root / str(metadata_path)) if metadata_path else {}
        result.append(
            {
                "table_id": table_id,
                "row_count": int(row_count or 0),
                "column_count": int(column_count or 0),
                "normalized_artifact_path": normalized_path,
                "table_index": int(metadata.get("table_index") or 0),
                "matrix": _matrix(workspace_root / str(normalized_path)) if normalized_path else None,
            }
        )
    return sorted(result, key=lambda value: (value["table_index"], value["table_id"]))


def _historical_ocr_metrics(
    registry: Registry,
    source_root: str,
    selected_paths: set[str],
) -> dict[str, Any]:
    """Summarize the latest per-page OCR runs for first-pass timing evidence."""

    cursor = registry.connection.execute(
        """
        SELECT source_relative_path, started_at, finished_at, status,
               timings_json, table_count, warnings_json
        FROM extraction_runs
        WHERE source_root=? AND attempted_route='ocr_rapidocr'
        ORDER BY finished_at DESC NULLS LAST, started_at DESC
        """,
        [source_root],
    )
    latest: dict[str, tuple[Any, ...]] = {}
    for row in cursor.fetchall():
        relative_path = str(row[0])
        if relative_path in selected_paths and relative_path not in latest:
            latest[relative_path] = row
    if not latest:
        return {"available": False, "reason": "no recorded OCR extraction runs"}

    render_ms = ocr_ms = image_table_ms = artifact_write_ms = 0.0
    table_assets = 0
    target_count = 0
    page_count = 0
    starts = []
    finishes = []
    statuses: dict[str, int] = {}
    for _relative_path, started, finished, status, timings_value, table_count, warnings_value in latest.values():
        statuses[str(status)] = statuses.get(str(status), 0) + 1
        if started is not None:
            starts.append(started)
        if finished is not None:
            finishes.append(finished)
        timings = timings_value
        if isinstance(timings, str):
            try:
                timings = json.loads(timings)
            except json.JSONDecodeError:
                timings = {}
        if not isinstance(timings, dict):
            timings = {}
        render_ms += float(timings.get("render_ms") or 0.0)
        ocr_ms += float(timings.get("ocr_ms") or 0.0)
        image_table_ms += float(timings.get("image_table_ms") or 0.0)
        artifact_write_ms += float(timings.get("artifact_write_ms") or 0.0)
        table_assets += int(table_count or 0)
        warnings = warnings_value
        if isinstance(warnings, str):
            try:
                warnings = json.loads(warnings)
            except json.JSONDecodeError:
                warnings = {}
        targets = warnings.get("ocr_targets", []) if isinstance(warnings, dict) else []
        if isinstance(targets, list):
            target_count += len(targets)
            page_count += sum(
                1 for target in targets
                if isinstance(target, dict) and target.get("kind") == "pdf_page"
            )
    wall_ms = 0.0
    if starts and finishes:
        wall_ms = max(0.0, (max(finishes) - min(starts)).total_seconds() * 1000.0)
    seconds = wall_ms / 1000.0 if wall_ms else 0.0
    return {
        "available": True,
        "files": len(latest),
        "reused": 0,
        "targets": target_count,
        "pages OCRed": page_count,
        "table assets": table_assets,
        "RapidOCR calls": target_count,
        "render ms": render_ms,
        "OCR ms": ocr_ms,
        "image table ms": image_table_ms,
        "artifact write ms": artifact_write_ms,
        "files/sec": len(latest) / seconds if seconds else 0.0,
        "pages/sec": page_count / seconds if seconds else 0.0,
        "wall clock ms": wall_ms,
        "statuses": statuses,
    }


def _cell_overlap(
    native: tuple[tuple[Any, ...], ...],
    image: tuple[tuple[Any, ...], ...],
) -> tuple[int, int]:
    overlap = 0
    denominator = max(
        len(native) * max((len(row) for row in native), default=0),
        len(image) * max((len(row) for row in image), default=0),
    )
    for row_index in range(min(len(native), len(image))):
        for column_index in range(min(len(native[row_index]), len(image[row_index]))):
            if normalize_cell(native[row_index][column_index]) == normalize_cell(image[row_index][column_index]):
                overlap += 1
    return overlap, denominator


def run_rendered_page_consistency(
    review_root: Path | str | None = None,
    *,
    max_pages: int = 12,
    workers: int | None = 1,
    force: bool = False,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> RenderedPageConsistencyResult:
    """Run the simulated-scan comparison and write an ignored JSON report."""

    root = Path(
        review_root
        if review_root is not None
        else paths.WORKSPACE_ROOT / "benchmark" / "pdf-real-v1" / "review"
    ).resolve()
    selected = select_rendered_pages(root, max_pages=max_pages)
    if not selected:
        raise ValueError(f"no rendered PNG pages found below {root}")
    workspace = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
    registry_file = Path(registry_path or paths.REGISTRY_PATH).resolve()
    before = {case.relative_path: _sha256(case.image_path) for case in selected}
    selected_paths = {case.relative_path for case in selected}
    ocr_summary = extract_ocr(
        root,
        workers=workers,
        force=force,
        registry_path=registry_file,
        workspace_root=workspace,
        selected_relative_paths=selected_paths,
    )
    source_root = canonical_source_root(root, require_directory=True)
    registry = Registry.open(registry_file)
    page_results: list[dict[str, Any]] = []
    table_count_consistent = 0
    shape_consistent = 0
    overlap_cells = 0
    overlap_denominator = 0
    first_run_metrics: dict[str, Any]
    reuse_run_metrics: dict[str, Any]
    try:
        text_cursor = registry.connection.execute(
            """
            SELECT t.source_relative_path
            FROM text_assets t
            JOIN files f ON f.file_id=t.file_id
            WHERE f.source_root=? AND t.extractor='rapidocr-onnx' AND t.is_current=TRUE
            """,
            [source_root],
        )
        ocr_text_paths = {str(row[0]) for row in text_cursor.fetchall()}
        first_run_metrics = _historical_ocr_metrics(registry, source_root, selected_paths)
        for case in selected:
            native_matrices = _native_matrices(case, workspace)
            image_tables = _image_tables(registry, source_root, case.relative_path, workspace)
            image_matrices = [item["matrix"] for item in image_tables if item["matrix"] is not None]
            count_equal = len(case.native_tables) == len(image_tables)
            table_count_consistent += int(count_equal)
            page_shape_matches = 0
            for native_matrix, image_matrix in zip(native_matrices, image_matrices):
                native_shape = (len(native_matrix), max((len(row) for row in native_matrix), default=0))
                image_shape = (len(image_matrix), max((len(row) for row in image_matrix), default=0))
                page_shape_matches += int(native_shape == image_shape)
                shape_consistent += int(native_shape == image_shape)
                overlap, denominator = _cell_overlap(native_matrix, image_matrix)
                overlap_cells += overlap
                overlap_denominator += denominator
            page_results.append(
                {
                    "sample_id": case.sample_id,
                    "relative_path": case.relative_path,
                    "page_number": case.page_number,
                    "native_candidate_hint": case.has_native_candidate,
                    "native_table_count": len(case.native_tables),
                    "image_table_count": len(image_tables),
                    "native_shapes": [
                        [len(matrix), max((len(row) for row in matrix), default=0)]
                        for matrix in native_matrices
                    ],
                    "image_shapes": [
                        [item["row_count"], item["column_count"]] for item in image_tables
                    ],
                    "table_count_consistent": count_equal,
                    "shape_matches_in_order": page_shape_matches,
                    "ocr_success": case.relative_path in ocr_text_paths,
                }
            )
    finally:
        registry.close()
    after = {case.relative_path: _sha256(case.image_path) for case in selected}
    source_unchanged = before == after
    report_root = workspace / "benchmark" / "pdf-real-v1"
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / "ocr-consistency.json"
    payload = {
        "benchmark_version": CONSISTENCY_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "review_root": str(root),
        "source_read_only": True,
        "source_unchanged": source_unchanged,
        "source_sha256": before,
        "comparison_is_not_accuracy": True,
        "native_result_is_weak_reference_only": True,
        "selection": {
            "max_pages": max_pages,
            "selected_pages": len(selected),
            "native_candidate_pages": sum(case.has_native_candidate for case in selected),
            "native_negative_pages": sum(not case.has_native_candidate for case in selected),
        },
        "ocr_summary": ocr_summary.benchmark_metrics(),
        "performance": {
            "first_run": first_run_metrics,
            "reuse_run": ocr_summary.benchmark_metrics() if ocr_summary.reused else {
                "available": False,
                "reason": "this invocation was not a reuse pass",
            },
        },
        "consistency": {
            "table_count_consistent_pages": table_count_consistent,
            "shape_consistent_tables": shape_consistent,
            "normalized_cell_overlap_cells": overlap_cells,
            "normalized_cell_overlap_denominator": overlap_denominator,
            "normalized_cell_overlap_ratio": (
                overlap_cells / overlap_denominator if overlap_denominator else 0.0
            ),
        },
        "pages": page_results,
        "llm_called": False,
        "network_allowed": False,
    }
    write_json_atomic(report_path, payload)
    return RenderedPageConsistencyResult(
        report_path=report_path,
        selected_pages=len(selected),
        native_candidate_pages=sum(case.has_native_candidate for case in selected),
        native_negative_pages=sum(not case.has_native_candidate for case in selected),
        ocr_summary=ocr_summary,
        source_unchanged=source_unchanged,
        table_count_consistent_pages=table_count_consistent,
        shape_consistent_tables=shape_consistent,
        normalized_cell_overlap_cells=overlap_cells,
        normalized_cell_overlap_ratio=overlap_cells / overlap_denominator if overlap_denominator else 0.0,
        first_run_metrics=first_run_metrics,
        reuse_run_metrics=(
            ocr_summary.benchmark_metrics()
            if ocr_summary.reused
            else {"available": False, "reason": "this invocation was not a reuse pass"}
        ),
    )
