"""Real-PDF baseline benchmark and human-review artifact generation.

This module intentionally does not contain another extractor.  It composes
the existing Phase 4A PyMuPDF and Phase 4B img2table candidate runners,
selects a deterministic sample from their registry facts, and writes only
review material below ``workspace/benchmark/pdf-real-v1``.  The source tree is
opened read-only and is never copied or modified.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Iterable

import polars as pl

from dongjian import paths
from dongjian.extract.pdf.artifacts import artifact_absolute, profile_target
from dongjian.extract.pdf.runner import PDFExtractionSummary, extract_pdf
from dongjian.extract.pdf.table_runner import PDFTableExtractionSummary, extract_pdf_tables
from dongjian.fingerprint import hash_file
from dongjian.registry import Registry, canonical_source_root


BENCHMARK_VERSION = "pdf-real-v1"
DEFAULT_MAX_SAMPLES = 80
DEFAULT_NEGATIVE_SAMPLES = 20
DEFAULT_WORKERS = 2


@dataclass(frozen=True)
class RealPDFBenchmarkResult:
    """Paths and measured summaries returned by the real-corpus workflow."""

    benchmark_root: Path
    manifest_path: Path
    review_path: Path
    report_path: Path
    source_integrity_path: Path
    sample_count: int
    native_summary: PDFExtractionSummary
    table_summary: PDFTableExtractionSummary


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> str:
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fieldnames})
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_pdf_source(source_root: Path) -> dict[str, dict[str, Any]]:
    """Hash every PDF and record stat values without creating source files."""

    snapshot: dict[str, dict[str, Any]] = {}
    for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            continue
        fingerprint = hash_file(path)
        relative = path.relative_to(source_root).as_posix()
        info = path.stat()
        snapshot[relative] = {
            "sha256": fingerprint.sha256,
            "size_bytes": int(info.st_size),
            "mtime_ns": int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        }
    return snapshot


def _summary_payload(summary: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in vars(summary).items():
        if isinstance(value, Path):
            payload[key] = str(value)
        elif hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
        else:
            payload[key] = value
    benchmark_metrics = getattr(summary, "benchmark_metrics", None)
    if callable(benchmark_metrics):
        payload["benchmark_metrics"] = benchmark_metrics()
    return payload


def _profile_for(registry: Registry, row: dict[str, Any]) -> dict[str, Any]:
    record = registry.current_pdf_profile(str(row["file_id"]), str(row["sha256"])) or {}
    profile = record.get("profile") if isinstance(record, dict) else None
    return dict(profile) if isinstance(profile, dict) else {}


def _candidate_pages(profile: dict[str, Any]) -> list[int]:
    pages: list[int] = []
    for item in profile.get("pages") or []:
        if isinstance(item, dict) and bool(item.get("possible_table_candidate")):
            try:
                pages.append(int(item["page_number"]))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(set(pages))


def _sample_id(file_id: str, sha256: str) -> str:
    return "pdfreal_" + hashlib.sha256(f"{file_id}:{sha256}".encode("utf-8")).hexdigest()[:16]


def _stable_rank(row: dict[str, Any]) -> tuple[str, str]:
    digest = hashlib.sha256(
        f"{row.get('file_id','')}:{row.get('sha256','')}".encode("utf-8")
    ).hexdigest()
    return digest, str(row.get("relative_path", "")).casefold()


def _select_samples(
    rows: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    *,
    max_samples: int,
    max_negative_samples: int,
) -> list[dict[str, Any]]:
    """Select candidates first, then deterministic native-text negatives.

    Candidate PDFs are all included when there are at most twenty of them,
    which is the important small-corpus rule for this source.  Larger
    corpora are capped by deterministic hash ordering after each profile class
    receives representation.  No random state is used.
    """

    decorated: list[dict[str, Any]] = []
    for row in rows:
        profile = profiles.get(str(row["file_id"]), {})
        classification = str(profile.get("classification") or "unknown")
        candidate_pages = _candidate_pages(profile)
        decorated.append(
            {
                **row,
                "profile": profile,
                "classification": classification,
                "candidate_pages": candidate_pages,
                "possible_table_candidate": bool(candidate_pages),
            }
        )

    candidates = [item for item in decorated if item["possible_table_candidate"]]
    negatives = [
        item
        for item in decorated
        if not item["possible_table_candidate"] and item["classification"] == "native_text"
    ]
    selected: list[dict[str, Any]] = []
    if len(candidates) <= 20:
        selected.extend(sorted(candidates, key=lambda item: str(item["relative_path"]).casefold()))
    else:
        # Preserve at least one file from every available profile class, then
        # fill the candidate allowance by stable SHA rank.
        for classification in ("native_text", "mixed", "suspected_scanned", "unknown"):
            class_candidates = sorted(
                [item for item in candidates if item["classification"] == classification],
                key=_stable_rank,
            )
            if class_candidates:
                selected.append(class_candidates[0])
        remaining = [item for item in sorted(candidates, key=_stable_rank) if item not in selected]
        selected.extend(remaining[: max(0, max_samples - len(selected))])

    # Negative controls are intentionally limited and only taken from native
    # PDFs without a profile table hint.
    for item in sorted(negatives, key=_stable_rank)[:max_negative_samples]:
        if len(selected) >= max_samples:
            break
        selected.append(item)

    # Ensure mixed/scanned/unknown files remain represented even when they had
    # no page-level candidate, then fill any remaining slots deterministically.
    for classification in ("mixed", "suspected_scanned", "unknown"):
        if len(selected) >= max_samples:
            break
        alternatives = sorted(
            [item for item in decorated if item["classification"] == classification],
            key=_stable_rank,
        )
        for item in alternatives:
            if item not in selected:
                selected.append(item)
                break
    if len(selected) < max_samples:
        for item in sorted(decorated, key=_stable_rank):
            if item not in selected:
                selected.append(item)
            if len(selected) >= max_samples:
                break

    selected_paths = {str(item["relative_path"]) for item in selected}
    result: list[dict[str, Any]] = []
    candidate_set = {str(item["relative_path"]) for item in candidates}
    negative_set = {str(item["relative_path"]) for item in negatives}
    for item in sorted(selected, key=lambda value: str(value["relative_path"]).casefold()):
        relative = str(item["relative_path"])
        if relative in candidate_set:
            reason = "all_table_candidate_pdfs" if len(candidates) <= 20 else "deterministic_candidate_stratum"
        elif relative in negative_set:
            reason = "native_text_negative_control"
        else:
            reason = f"profile_class_{item['classification']}_coverage"
        result.append(
            {
                **item,
                "sample_id": _sample_id(str(item["file_id"]), str(item["sha256"])),
                "selected_reason": reason,
            }
        )
    return result


def _table_rows(registry: Registry, sample: dict[str, Any]) -> list[dict[str, Any]]:
    cursor = registry.connection.execute(
        """
        SELECT table_id, page_number, row_count, column_count,
               raw_artifact_path, normalized_artifact_path,
               metadata_artifact_path, extraction_confidence, quality_status
        FROM table_assets
        WHERE file_id=? AND content_sha256=? AND extractor='img2table-candidate' AND is_current=TRUE
        ORDER BY page_number NULLS LAST, table_id
        """,
        [sample["file_id"], sample["sha256"]],
    )
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _page_layout_hint(page: dict[str, Any], detected_count: int) -> str:
    """Return an explicitly non-evaluative hint for reviewer navigation."""

    chars = int(page.get("effective_chars") or page.get("total_chars") or 0)
    if detected_count and chars >= 250:
        return "webpage_like"
    if detected_count:
        return "table_dominant"
    if chars:
        return "document"
    return "other"


def _render_pages(source: Path, pages: list[int], output_dir: Path) -> list[str]:
    if not pages:
        return []
    import pymupdf

    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[str] = []
    document = pymupdf.open(str(source))
    try:
        for page_number in pages:
            if page_number < 1 or page_number > document.page_count:
                continue
            target = output_dir / f"page-{page_number:04d}.png"
            page = document[page_number - 1]
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.35, 1.35), alpha=False)
            pixmap.save(str(target))
            rendered.append(target.name)
    finally:
        document.close()
    return rendered


def _write_review_artifacts(
    *,
    source_root: Path,
    benchmark_root: Path,
    samples: list[dict[str, Any]],
    registry: Registry,
) -> list[dict[str, Any]]:
    review_rows: list[dict[str, Any]] = []
    for sample in samples:
        sample_dir = benchmark_root / "review" / str(sample["sample_id"])
        sample_dir.mkdir(parents=True, exist_ok=True)
        profile = dict(sample.get("profile") or {})
        tables = _table_rows(registry, sample)
        pages_by_number: dict[int, list[dict[str, Any]]] = {}
        for table in tables:
            try:
                page_number = int(table.get("page_number") or 0)
            except (TypeError, ValueError):
                page_number = 0
            pages_by_number.setdefault(page_number, []).append(table)
        profile_pages = {
            int(page["page_number"]): page
            for page in profile.get("pages") or []
            if isinstance(page, dict) and str(page.get("page_number", "")).isdigit()
        }
        relevant_pages = sorted(set(_candidate_pages(profile)) | set(pages_by_number))
        source_path = source_root / Path(str(sample["relative_path"]))
        rendered = _render_pages(source_path, relevant_pages, sample_dir / "pages")

        original_profile_path = profile_target(paths.WORKSPACE_ROOT, str(sample["file_id"]), str(sample["sha256"]))
        profile_payload = profile
        if original_profile_path.is_file():
            try:
                profile_payload = json.loads(original_profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                profile_payload = profile
        profile_payload = {
            **profile_payload,
            "benchmark_sample_id": sample["sample_id"],
            "benchmark_version": BENCHMARK_VERSION,
        }
        _write_json(sample_dir / "profile.json", profile_payload)
        _write_json(
            sample_dir / "detected_tables.json",
            {
                "candidate_status": "candidate",
                "source_relative_path": sample["relative_path"],
                "tables": tables,
                "rendered_pages": rendered,
            },
        )
        _write_json(
            sample_dir / "provenance.json",
            {
                "sample_id": sample["sample_id"],
                "file_id": sample["file_id"],
                "content_sha256": sample["sha256"],
                "source_root": str(source_root),
                "source_relative_path": sample["relative_path"],
                "profile_class": sample["classification"],
                "possible_table_candidate": sample["possible_table_candidate"],
                "candidate_pages": sample["candidate_pages"],
                "extractor": "img2table-candidate",
                "ocr_enabled": False,
                "tables": tables,
            },
        )
        for index, table in enumerate(tables, start=1):
            normalized_path = table.get("normalized_artifact_path")
            if not normalized_path:
                continue
            try:
                frame = pl.read_parquet(artifact_absolute(str(normalized_path), paths.WORKSPACE_ROOT))
            except (OSError, ValueError, RuntimeError):
                continue
            frame.write_csv(sample_dir / f"table-{index:03d}-preview.csv")
            _write_json(sample_dir / f"table-{index:03d}-preview.json", frame.to_dicts())

        for page_number in relevant_pages:
            page = profile_pages.get(page_number, {})
            page_tables = pages_by_number.get(page_number, [])
            common = {
                "sample_id": sample["sample_id"],
                "relative_path": sample["relative_path"],
                "page_number": page_number,
                "detected_table_count": len(page_tables),
                "page_layout": "",  # Human review field; the hint lives in the artifact below.
                "expected_table_count": "",
                "detection_correct": "",
                "structure_quality": "",
                "content_quality": "",
                "false_positive": "",
                "false_negative": "",
                "notes": "",
                "reviewed": "",
            }
            auto_hint = _page_layout_hint(page, len(page_tables))
            if page_tables:
                for table in page_tables:
                    review_rows.append(
                        {
                            **common,
                            "table_index": table.get("table_id", ""),
                            "auto_page_layout_hint": auto_hint,
                        }
                    )
            else:
                review_rows.append(
                    {
                        **common,
                        "table_index": "",
                        "auto_page_layout_hint": auto_hint,
                    }
                )
    return review_rows


def _registry_history(registry: Registry, source_root: str, route: str) -> list[dict[str, Any]]:
    if route == "scan_runs":
        cursor = registry.connection.execute(
            """
            SELECT run_id, source_root, status, started_at, finished_at,
                   discovered_count, hashed_count, reused_hash_count,
                   failed_count, total_bytes, elapsed_ms, hashing_ms,
                   detection_ms, registry_write_ms
            FROM scan_runs WHERE source_root=? ORDER BY started_at, run_id
            """,
            [source_root],
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    cursor = registry.connection.execute(
        """
        SELECT extraction_run_id, file_id, source_relative_path, status,
               started_at, finished_at, force, timings_json, table_count,
               quality_issue_count, total_rows, total_bytes
        FROM extraction_runs
        WHERE source_root=? AND attempted_route=?
        ORDER BY started_at, extraction_run_id
        """,
        [source_root, route],
    )
    columns = [item[0] for item in cursor.description]
    rows: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        item = dict(zip(columns, row))
        timings = item.get("timings_json")
        if isinstance(timings, str):
            try:
                item["timings"] = json.loads(timings)
            except json.JSONDecodeError:
                item["timings"] = {}
        else:
            item["timings"] = timings or {}
        item.pop("timings_json", None)
        rows.append(item)
    return rows


def _profile_distribution(rows: list[dict[str, Any]], profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    classes: dict[str, int] = {}
    page_counts: list[int] = []
    size_bytes: list[int] = []
    candidate_files = 0
    candidate_pages = 0
    total_chars = 0
    for row in rows:
        profile = profiles.get(str(row["file_id"]), {})
        classification = str(profile.get("classification") or "unknown")
        classes[classification] = classes.get(classification, 0) + 1
        page_counts.append(int(profile.get("page_count") or 0))
        size_bytes.append(int(row.get("size_bytes") or 0))
        total_chars += int(profile.get("total_chars") or 0)
        pages = _candidate_pages(profile)
        if pages:
            candidate_files += 1
            candidate_pages += len(pages)
    return {
        "pdf_count": len(rows),
        "profile_class_counts": classes,
        "page_count_distribution": {
            "min": min(page_counts) if page_counts else 0,
            "max": max(page_counts) if page_counts else 0,
            "total": sum(page_counts),
            "values": sorted(page_counts),
        },
        "file_size_distribution": {
            "min_bytes": min(size_bytes) if size_bytes else 0,
            "max_bytes": max(size_bytes) if size_bytes else 0,
            "total_bytes": sum(size_bytes),
            "values": sorted(size_bytes),
        },
        "native_text_chars": total_chars,
        "possible_table_candidate_files": candidate_files,
        "possible_table_candidate_pages": candidate_pages,
    }


def _first_reuse_scan(scan_history: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not scan_history:
        return None
    first = scan_history[0]
    for item in scan_history[1:]:
        if int(item.get("reused_hash_count") or 0) > 0:
            return item
    return first


def run_real_pdf_benchmark(
    source: Path | str,
    *,
    workers: int | None = None,
    max_samples: int = DEFAULT_MAX_SAMPLES,
    max_negative_samples: int = DEFAULT_NEGATIVE_SAMPLES,
    force: bool = False,
) -> RealPDFBenchmarkResult:
    """Run the real-PDF baseline and publish deterministic review material."""

    source_root = Path(canonical_source_root(source, require_directory=True))
    if max_samples < 1:
        raise ValueError("max_samples must be positive")
    benchmark_root = paths.WORKSPACE_ROOT / "benchmark" / BENCHMARK_VERSION
    benchmark_root.mkdir(parents=True, exist_ok=True)
    before = _snapshot_pdf_source(source_root)

    native_summary = extract_pdf(
        source_root,
        workers=workers or DEFAULT_WORKERS,
        force=force,
        registry_path=paths.REGISTRY_PATH,
        workspace_root=paths.WORKSPACE_ROOT,
    )

    registry = Registry.open(paths.REGISTRY_PATH)
    try:
        rows = registry.pdf_candidates(str(source_root))
        profiles = {str(row["file_id"]): _profile_for(registry, row) for row in rows}
        samples = _select_samples(
            rows,
            profiles,
            max_samples=max_samples,
            max_negative_samples=max_negative_samples,
        )
    finally:
        registry.close()

    selected_paths = {str(item["relative_path"]) for item in samples}
    table_summary = extract_pdf_tables(
        source_root,
        workers=workers or DEFAULT_WORKERS,
        force=force,
        registry_path=paths.REGISTRY_PATH,
        workspace_root=paths.WORKSPACE_ROOT,
        selected_relative_paths=selected_paths,
    )

    registry = Registry.open(paths.REGISTRY_PATH)
    try:
        manifest_rows = [
            {
                "sample_id": item["sample_id"],
                "file_id": item["file_id"],
                "sha256": item["sha256"],
                "relative_path": item["relative_path"],
                "page_count": item["profile"].get("page_count", 0),
                "profile_class": item["classification"],
                "possible_table_candidate": str(bool(item["possible_table_candidate"])).lower(),
                "size_bytes": item["size_bytes"],
                "selected_reason": item["selected_reason"],
            }
            for item in samples
        ]
        manifest_path = benchmark_root / "manifest.csv"
        _write_csv(
            manifest_path,
            [
                "sample_id",
                "file_id",
                "sha256",
                "relative_path",
                "page_count",
                "profile_class",
                "possible_table_candidate",
                "size_bytes",
                "selected_reason",
            ],
            manifest_rows,
        )
        review_rows = _write_review_artifacts(
            source_root=source_root,
            benchmark_root=benchmark_root,
            samples=samples,
            registry=registry,
        )
    finally:
        registry.close()

    review_path = benchmark_root / "review.csv"
    _write_csv(
        review_path,
        [
            "sample_id",
            "relative_path",
            "page_number",
            "table_index",
            "detected_table_count",
            "page_layout",
            "auto_page_layout_hint",
            "expected_table_count",
            "detection_correct",
            "structure_quality",
            "content_quality",
            "false_positive",
            "false_negative",
            "notes",
            "reviewed",
        ],
        review_rows,
    )

    after = _snapshot_pdf_source(source_root)
    integrity = {
        "source_root": str(source_root),
        "captured_at": _now_iso(),
        "before": before,
        "after": after,
        "unchanged": before == after,
        "changed_paths": sorted(set(before) ^ set(after))
        + sorted(key for key in set(before) & set(after) if before[key] != after[key]),
    }
    source_integrity_path = benchmark_root / "source_integrity.json"
    _write_json(source_integrity_path, integrity)

    registry = Registry.open(paths.REGISTRY_PATH)
    try:
        scan_history = _registry_history(registry, str(source_root), "scan_runs")
        native_history = _registry_history(registry, str(source_root), "pdf_native_text")
        table_history = _registry_history(registry, str(source_root), "pdf_table_candidate")
    finally:
        registry.close()

    class_counts: dict[str, int] = {}
    candidate_file_count = 0
    candidate_page_count = 0
    for item in samples:
        class_counts[item["classification"]] = class_counts.get(item["classification"], 0) + 1
        if item["possible_table_candidate"]:
            candidate_file_count += 1
            candidate_page_count += len(item["candidate_pages"])

    corpus_distribution = _profile_distribution(rows, profiles)
    report = {
        "benchmark_version": BENCHMARK_VERSION,
        "created_at": _now_iso(),
        "source_root": str(source_root),
        "source_read_only": True,
        "source_integrity": integrity,
        "selection": {
            "max_samples": max_samples,
            "max_negative_samples": max_negative_samples,
            "sample_count": len(samples),
            "profile_class_counts": class_counts,
            "candidate_sample_files": candidate_file_count,
            "candidate_sample_pages": candidate_page_count,
            "negative_controls": sum(1 for item in samples if item["selected_reason"] == "native_text_negative_control"),
        },
        "corpus_distribution": corpus_distribution,
        "native_profile_current_run": _summary_payload(native_summary),
        "table_candidate_current_run": _summary_payload(table_summary),
        "registry_history": {
            "native_text": native_history,
            "table_candidate": table_history,
            "scan_runs": scan_history,
            "first_scan": scan_history[0] if scan_history else None,
            "incremental_reuse_scan": _first_reuse_scan(scan_history),
        },
        "review": {
            "manifest": str(manifest_path.relative_to(paths.WORKSPACE_ROOT)),
            "review_csv": str(review_path.relative_to(paths.WORKSPACE_ROOT)),
            "review_root": str((benchmark_root / "review").relative_to(paths.WORKSPACE_ROOT)),
            "human_ground_truth_required": True,
            "unreviewed_fields_are_blank": True,
            "rows": len(review_rows),
        },
        "llm_called": False,
        "ocr_called": False,
        "candidate_status": "img2table-candidate-native-text-only",
    }
    report_path = benchmark_root / "report.json"
    _write_json(report_path, report)
    return RealPDFBenchmarkResult(
        benchmark_root=benchmark_root,
        manifest_path=manifest_path,
        review_path=review_path,
        report_path=report_path,
        source_integrity_path=source_integrity_path,
        sample_count=len(samples),
        native_summary=native_summary,
        table_summary=table_summary,
    )
