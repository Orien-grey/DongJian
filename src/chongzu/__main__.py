"""Command-line entry point for ``python -m chongzu``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, paths
from .doctor import main as doctor_main
from .registry import Registry, RegistryError, canonical_source_root
from .scan import ScanError, benchmark_metrics, scan_source
from .extract import (
    PDFExtractionError,
    PDFTableExtractionError,
    StructuredExtractionError,
    OCRExtractionError,
    UnifiedExtractionError,
    extract_pdf,
    extract_pdf_tables,
    extract_structured,
    extract_ocr,
    extract_unified,
)
from .extract.pdf.table_quality import load_ground_truth
from .extract.artifacts import artifact_absolute
from .benchmark.pdf_real import run_real_pdf_benchmark
from .benchmark.ocr_consistency import run_rendered_page_consistency
from .clean import CleaningError, process_source
from .search import SearchQuery, SearchService, run_search_benchmark
from .services.sql import SqlQueryService, run_sql_benchmark
from .semantic.runner import (
    RealSemanticProviderDisabled,
    SemanticNotConfigured,
    enrich_catalog,
    semantic_status,
)


def _print_summary(summary) -> None:
    print(f"Run: {summary.run_id}")
    print(f"Source: {summary.source_root}")
    print(f"Discovered: {summary.discovered_count}")
    print(f"New: {summary.new_count}")
    print(f"Changed: {summary.changed_count}")
    print(f"Unchanged: {summary.unchanged_count}")
    print(f"Missing: {summary.missing_count}")
    print(f"Exact duplicate paths: {summary.exact_duplicate_paths}")
    print(f"Hashed: {summary.hashed_count}")
    print(f"Reused hashes: {summary.reused_hash_count}")
    print(f"Failed: {summary.failed_count}")
    print(f"Elapsed: {summary.elapsed_ms:.2f} ms")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m chongzu")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="check the project-local runtime")
    scan_parser = sub.add_parser("scan", help="read-only scan of a source directory")
    scan_parser.add_argument("source", type=Path)
    scan_parser.add_argument("--workers", type=int, default=None)
    scan_parser.add_argument("--rehash", action="store_true")

    extract_parser = sub.add_parser("extract", help="extract assets from registry-backed source files")
    extract_sub = extract_parser.add_subparsers(dest="extract_command", required=True)
    extract_structured_parser = extract_sub.add_parser("structured", help="extract CSV/TSV/XLS/XLSX tables")
    extract_structured_parser.add_argument("source", type=Path)
    extract_structured_parser.add_argument("--workers", type=int, default=None)
    extract_structured_parser.add_argument("--force", action="store_true")
    extract_pdf_parser = extract_sub.add_parser("pdf", help="extract native PDF text and profiles")
    extract_pdf_parser.add_argument("source", type=Path)
    extract_pdf_parser.add_argument("--workers", type=int, default=None)
    extract_pdf_parser.add_argument("--force", action="store_true")
    extract_pdf_table_parser = extract_sub.add_parser(
        "pdf-table", help="extract native-text PDF table candidates (img2table)"
    )
    extract_pdf_table_parser.add_argument("source", type=Path)
    extract_pdf_table_parser.add_argument("--workers", type=int, default=None)
    extract_pdf_table_parser.add_argument("--force", action="store_true")
    extract_pdf_table_parser.add_argument("--ground-truth", type=Path, default=None)
    extract_ocr_parser = extract_sub.add_parser("ocr", help="offline RapidOCR for images and scanned PDF pages")
    extract_ocr_parser.add_argument("source", type=Path)
    extract_ocr_parser.add_argument("--workers", type=int, default=None)
    extract_ocr_parser.add_argument("--force", action="store_true")
    extract_unified_parser = extract_sub.add_parser(
        "unified", help="formal dual table/text extraction pipeline"
    )
    extract_unified_parser.add_argument("source", type=Path)
    extract_unified_parser.add_argument("--workers", type=int, default=None)
    extract_unified_parser.add_argument("--force", action="store_true")

    process_parser = sub.add_parser("process", help="extract, clean, profile, and catalog a source")
    process_parser.add_argument("source", type=Path)
    process_parser.add_argument("--workers", type=int, default=None)
    process_parser.add_argument("--force", action="store_true")
    process_parser.add_argument(
        "--drop-exact-duplicates",
        action="store_true",
        help="drop exact duplicate rows in the derived normalized artifact",
    )

    semantic_parser = sub.add_parser("semantic", help="explicit optional semantic enrichment")
    semantic_sub = semantic_parser.add_subparsers(dest="semantic_command", required=True)
    semantic_sub.add_parser("status", help="show provider configuration without making a network call")
    semantic_enrich_parser = semantic_sub.add_parser(
        "enrich", help="enrich catalog assets; Phase 7A permits only explicit --provider fake"
    )
    semantic_enrich_parser.add_argument("--source", type=Path, default=None)
    semantic_enrich_parser.add_argument("--asset", dest="asset_id", default=None)
    semantic_enrich_parser.add_argument("--type", dest="asset_type", choices=("table", "text"), default=None)
    semantic_enrich_parser.add_argument(
        "--provider",
        choices=("openai-compatible", "fake"),
        default="openai-compatible",
        help="openai-compatible is disabled in Phase 7A; fake is an explicit offline test provider",
    )
    semantic_enrich_parser.add_argument("--force", action="store_true")
    semantic_enrich_parser.add_argument("--limit", type=int, default=None)

    registry_parser = sub.add_parser("registry", help="inspect the local DuckDB registry")
    registry_sub = registry_parser.add_subparsers(dest="registry_command", required=True)
    summary_parser = registry_sub.add_parser("summary")
    summary_parser.add_argument("--source", type=Path, default=None)
    files_parser = registry_sub.add_parser("files")
    files_parser.add_argument("--source", type=Path, default=None)
    files_parser.add_argument("--state", default=None)
    files_parser.add_argument("--limit", type=int, default=1000)

    catalog_parser = sub.add_parser("catalog", help="query the local cleaned asset catalog")
    catalog_sub = catalog_parser.add_subparsers(dest="catalog_command", required=True)
    catalog_summary_parser = catalog_sub.add_parser("summary")
    catalog_summary_parser.add_argument("--source", type=Path, default=None)
    catalog_list_parser = catalog_sub.add_parser("list")
    catalog_list_parser.add_argument("--source", type=Path, default=None)
    catalog_list_parser.add_argument("--type", dest="asset_type", choices=("table", "text"), default=None)
    catalog_list_parser.add_argument(
        "--quality", dest="quality_status", choices=("ready", "needs_review", "unusable"), default=None
    )
    catalog_list_parser.add_argument("--format", dest="source_format", default=None)
    catalog_list_parser.add_argument("--limit", type=int, default=100)
    catalog_show_parser = catalog_sub.add_parser("show")
    catalog_show_parser.add_argument("asset_id")
    catalog_show_parser.add_argument("--rows", type=int, default=20)
    catalog_show_parser.add_argument("--chars", type=int, default=2000)

    search_parser = sub.add_parser("search", help="search local catalog metadata and text chunks")
    search_parser.add_argument("query")
    search_parser.add_argument("--type", dest="asset_type", choices=("all", "table", "text"), default="all")
    search_parser.add_argument("--format", dest="source_format", default=None)
    search_parser.add_argument(
        "--quality", dest="quality_status", choices=("ready", "needs_review", "unusable"), default=None
    )
    search_parser.add_argument("--match", choices=("all", "phrase"), default="all")
    search_parser.add_argument("--limit", type=int, default=30)

    benchmark_parser = sub.add_parser("benchmark", help="measure extraction throughput")
    benchmark_sub = benchmark_parser.add_subparsers(dest="benchmark_command", required=True)
    benchmark_scan = benchmark_sub.add_parser("scan")
    benchmark_scan.add_argument("source", type=Path)
    benchmark_scan.add_argument("--workers", type=int, default=None)
    benchmark_scan.add_argument("--rehash", action="store_true")
    benchmark_structured = benchmark_sub.add_parser("structured", help="measure structured extraction throughput")
    benchmark_structured.add_argument("source", type=Path)
    benchmark_structured.add_argument("--workers", type=int, default=None)
    benchmark_structured.add_argument("--force", action="store_true")
    benchmark_pdf = benchmark_sub.add_parser("pdf", help="measure native PDF text extraction throughput")
    benchmark_pdf.add_argument("source", type=Path)
    benchmark_pdf.add_argument("--workers", type=int, default=None)
    benchmark_pdf.add_argument("--force", action="store_true")
    benchmark_pdf_table = benchmark_sub.add_parser(
        "pdf-table", help="benchmark native-text PDF table candidates (img2table)"
    )
    benchmark_pdf_table.add_argument("source", type=Path)
    benchmark_pdf_table.add_argument("--workers", type=int, default=None)
    benchmark_pdf_table.add_argument("--force", action="store_true")
    benchmark_pdf_table.add_argument("--ground-truth", type=Path, default=None)
    benchmark_pdf_real = benchmark_sub.add_parser(
        "pdf-real", help="profile a real PDF corpus and publish review artifacts"
    )
    benchmark_pdf_real.add_argument("source", type=Path)
    benchmark_pdf_real.add_argument("--workers", type=int, default=None)
    benchmark_pdf_real.add_argument("--max-samples", type=int, default=80)
    benchmark_pdf_real.add_argument("--max-negative-samples", type=int, default=20)
    benchmark_pdf_real.add_argument("--force", action="store_true")
    benchmark_pdf_consistency = benchmark_sub.add_parser(
        "pdf-consistency", help="compare rendered PDF pages with the offline image route"
    )
    benchmark_pdf_consistency.add_argument("review_root", type=Path, nargs="?", default=None)
    benchmark_pdf_consistency.add_argument("--max-pages", type=int, default=12)
    benchmark_pdf_consistency.add_argument("--workers", type=int, default=1)
    benchmark_pdf_consistency.add_argument("--force", action="store_true")
    benchmark_ocr = benchmark_sub.add_parser("ocr", help="benchmark offline RapidOCR image/scanned-page extraction")
    benchmark_ocr.add_argument("source", type=Path)
    benchmark_ocr.add_argument("--workers", type=int, default=None)
    benchmark_ocr.add_argument("--force", action="store_true")
    benchmark_cleaning = benchmark_sub.add_parser(
        "cleaning", help="benchmark deterministic cleaning and profiling"
    )
    benchmark_cleaning.add_argument("source", type=Path)
    benchmark_cleaning.add_argument("--workers", type=int, default=None)
    benchmark_cleaning.add_argument("--force", action="store_true")
    benchmark_cleaning.add_argument("--drop-exact-duplicates", action="store_true")
    benchmark_search = benchmark_sub.add_parser("search", help="benchmark local lexical retrieval")
    benchmark_search.add_argument("queries", nargs="*", default=None)
    benchmark_search.add_argument("--format", dest="source_format", default=None)
    benchmark_sub.add_parser("sql", help="benchmark bounded local SQL")
    return parser


def _print_structured_summary(summary) -> None:
    print(f"Source: {summary.source_root}")
    print(f"Files considered: {summary.files_considered}")
    print(f"Structured supported: {summary.structured_supported}")
    print(f"Extracted: {summary.extracted}")
    print(f"Reused: {summary.reused}")
    print(f"Tables produced: {summary.tables_produced}")
    print(f"Quality issues: {summary.quality_issues}")
    print(f"Failed: {summary.failed}")
    print(f"Total rows: {summary.total_rows}")
    print(f"Total bytes: {summary.total_bytes}")
    print(f"Elapsed: {summary.wall_time_ms:.2f} ms")


def _print_pdf_summary(summary) -> None:
    print(f"Source: {summary.source_root}")
    print(f"Files considered: {summary.files_considered}")
    print(f"PDF files: {summary.pdf_files}")
    print(f"Extracted: {summary.extracted}")
    print(f"Reused: {summary.reused}")
    print(f"Text assets produced: {summary.text_assets_produced}")
    print(f"Quality issues: {summary.quality_issues}")
    print(f"Failed PDFs: {summary.failed_pdfs}")
    print(f"Pages: {summary.pages}")
    print(f"Total chars: {summary.total_chars}")
    print(f"Total bytes: {summary.total_bytes}")
    print(f"Native-text PDFs: {summary.native_text_pdfs}")
    print(f"Mixed PDFs: {summary.mixed_pdfs}")
    print(f"Suspected-scanned PDFs: {summary.suspected_scanned_pdfs}")
    print(f"Unknown PDFs: {summary.unknown_pdfs}")
    print(f"Elapsed: {summary.wall_time_ms:.2f} ms")


def _print_pdf_table_summary(summary) -> None:
    print(f"Source: {summary.source_root}")
    print("Candidate: img2table (native-text only)")
    print("OCR: disabled")
    print(f"PDFs considered: {summary.pdfs_considered}")
    print(f"PDFs attempted: {summary.pdfs_attempted}")
    print(f"Reused: {summary.reused}")
    print(f"Deferred to OCR: {summary.deferred_to_ocr}")
    print(f"Deferred pages: {summary.deferred_pages}")
    print(f"Pages: {summary.pages}")
    print(f"Tables detected: {summary.detected_tables}")
    print(f"Table assets: {summary.table_assets}")
    print(f"Rows: {summary.rows}")
    print(f"Cells: {summary.cells}")
    print(f"Quality issues: {summary.quality_issues}")
    print(f"Extraction failures: {summary.extraction_failures}")
    print(f"Elapsed: {summary.wall_time_ms:.2f} ms")


def _load_ground_truth(path: Path | None):
    return load_ground_truth(path) if path is not None else None


def _print_ocr_summary(summary) -> None:
    print(f"Source: {summary.source_root}")
    print("Extractor: RapidOCR + ONNX Runtime (offline, CPU)")
    print(f"Files considered: {summary.files_considered}")
    print(f"Images considered: {summary.images_considered}")
    print(f"PDFs considered: {summary.pdfs_considered}")
    print(f"Files attempted: {summary.files_attempted}")
    print(f"Extracted: {summary.extracted}")
    print(f"Reused: {summary.reused}")
    print(f"Targets: {summary.targets}")
    print(f"Pages OCRed: {summary.pages_ocred}")
    print(f"Text assets produced: {summary.text_assets_produced}")
    print(f"Table assets produced: {summary.table_assets_produced}")
    print(f"Image table extraction failures: {summary.image_table_extraction_failures}")
    print(f"RapidOCR calls: {summary.image_table_ocr_calls}")
    print(f"OCR chars: {summary.ocr_chars}")
    print(f"Deferred to profile: {summary.deferred_to_profile}")
    print(f"Quality issues: {summary.quality_issues}")
    print(f"Failures: {summary.failures}")
    print(f"Elapsed: {summary.wall_time_ms:.2f} ms")


def _print_unified_summary(summary) -> None:
    print(f"Run: {summary.run_id}")
    print(f"Source: {summary.source_root}")
    print(f"Files discovered: {summary.files_discovered}")
    print(f"Supported: {summary.supported}")
    print(f"Unsupported: {summary.unsupported}")
    print(f"Processed: {summary.processed}")
    print(f"Reused: {summary.reused}")
    print(f"Failed: {summary.failed}")
    print(f"TableAssets: {summary.table_assets}")
    print(f"TextAssets: {summary.text_assets}")
    print(f"TextChunks: {summary.text_chunks}")
    print(f"QualityIssues: {summary.quality_issues}")
    print(f"Structured files: {summary.structured_files}")
    print(f"Native PDF pages: {summary.native_pdf_pages}")
    print(f"OCR pages/images: {summary.ocr_pages_images}")
    print(f"Deferred: {summary.deferred}")
    print(f"Wall time: {summary.wall_time_ms:.2f} ms")


def _print_process_summary(summary) -> None:
    print(f"Source: {summary.source_root}")
    print(f"Files discovered: {summary.files_discovered}")
    print(f"Files supported: {summary.files_supported}")
    print(f"Files unsupported: {summary.files_unsupported}")
    print(f"Extracted: {summary.extracted}")
    print(f"Reused extraction: {summary.reused_extraction}")
    print(f"Extraction failures: {summary.extraction_failures}")
    print(f"Table assets: {summary.table_assets}")
    print(f"Text assets: {summary.text_assets}")
    print(f"Cleaned: {summary.cleaned}")
    print(f"Reused cleaning: {summary.reused_cleaning}")
    print(f"Cleaning failures: {summary.cleaning_failures}")
    print(f"Ready: {summary.ready}")
    print(f"Needs review: {summary.needs_review}")
    print(f"Unusable: {summary.unusable}")
    print(f"Quality issues: {summary.quality_issues}")
    print(f"Semantic pending: {summary.semantic_pending}")
    print(f"Total wall time: {summary.wall_time_ms:.2f} ms")


def _print_semantic_summary(summary) -> None:
    print(f"Provider: {summary.provider}")
    print(f"Model: {summary.model}")
    print(f"Assets considered: {summary.assets_considered}")
    print(f"Attempted: {summary.attempted}")
    print(f"Enriched: {summary.enriched}")
    print(f"Reused: {summary.reused}")
    print(f"Failed: {summary.failed}")
    print(f"Warnings: {summary.warnings}")
    print(f"Quality suggestions: {summary.quality_suggestions}")
    print(f"Source chars: {summary.source_chars}")
    print(f"Sent chars: {summary.sent_chars}")
    print(f"Sampled rows: {summary.sampled_rows}")
    print(f"Inputs truncated: {summary.input_truncated}")
    print(f"Wall time: {summary.wall_time_ms:.2f} ms")
    for failure in summary.failures[:20]:
        print(f"Failure: {failure.get('asset_id')} [{failure.get('error_code')}] {failure.get('error')}")


def _read_json_artifact(path_value: object):
    if not path_value:
        return None
    try:
        path = artifact_absolute(str(path_value), paths.WORKSPACE_ROOT)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _catalog_show_payload(details: dict[str, object], *, rows: int, chars: int) -> dict[str, object]:
    if rows < 1 or rows > 1000 or chars < 1 or chars > 100_000:
        raise ValueError("preview limits must be between 1 and 1000 rows or 100000 characters")
    payload: dict[str, object] = {
        "asset": details,
        "metadata": _read_json_artifact(details.get("metadata_artifact_path")),
        "profile": details.get("profile"),
        "quality_issues": details.get("quality_issues", []),
    }
    if details.get("asset_type") == "table":
        normalized = details.get("normalized_artifact_path")
        preview: list[object] = []
        if normalized:
            try:
                import polars as pl

                path = artifact_absolute(str(normalized), paths.WORKSPACE_ROOT)
                if path.is_file():
                    preview = pl.scan_parquet(path).head(rows).collect().to_dicts()
            except (OSError, ValueError, RuntimeError):
                preview = []
        payload["preview"] = preview
    else:
        normalized = details.get("normalized_artifact_path")
        preview = ""
        if normalized:
            try:
                path = artifact_absolute(str(normalized), paths.WORKSPACE_ROOT)
                if path.is_file():
                    preview = path.read_text(encoding="utf-8")[:chars]
            except (OSError, ValueError, UnicodeError):
                preview = ""
        payload["normalized_text_preview"] = preview
    return payload


def _print_catalog_summary(summary: dict[str, int]) -> None:
    print(f"Files: {summary.get('files', 0)}")
    print(f"TableAssets: {summary.get('table_assets', 0)}")
    print(f"TextAssets: {summary.get('text_assets', 0)}")
    print(f"TextChunks: {summary.get('text_chunks', 0)}")
    print(f"Ready: {summary.get('ready', 0)}")
    print(f"Needs review: {summary.get('needs_review', 0)}")
    print(f"Unusable: {summary.get('unusable', 0)}")
    print(f"Quality issues: {summary.get('quality_issues', 0)}")
    print(f"Semantic pending: {summary.get('semantic_pending', 0)}")


def _print_search_results(response) -> None:
    print(f"Query: {response.query}")
    print(f"Results: {len(response.results)} / {response.total}")
    for result in response.results:
        location = []
        if result.page_number is not None:
            location.append(f"page {result.page_number}")
        if result.sheet_name:
            location.append(f"sheet {result.sheet_name}")
        location_text = f" ({', '.join(location)})" if location else ""
        print(f"[{result.match_kind}] {result.display_name} [{result.asset_id}]")
        print(f"  {result.source_file}{location_text} | score={result.score:.2f}")
        if result.snippet:
            print(f"  {result.snippet}")


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch diagnostics, registry inspection, and extraction commands."""

    args = list(sys.argv[1:] if argv is None else argv)
    # ``extract SOURCE`` is the formal user entry point.  The existing named
    # subcommands remain expert/debug routes, so normalize only an unknown
    # second token into the explicit parser branch.
    expert_extract_commands = {"structured", "pdf", "pdf-table", "ocr", "unified"}
    if len(args) >= 2 and args[0] == "extract" and args[1] not in expert_extract_commands and not args[1].startswith("-"):
        args = ["extract", "unified", *args[1:]]
    parser = _build_parser()
    parsed = parser.parse_args(args)
    try:
        if parsed.command == "doctor":
            return doctor_main([])
        if parsed.command == "scan":
            summary = scan_source(parsed.source, workers=parsed.workers, rehash=parsed.rehash)
            _print_summary(summary)
            return 0 if summary.status == "complete" else 1
        if parsed.command == "extract" and parsed.extract_command == "structured":
            summary = extract_structured(parsed.source, workers=parsed.workers, force=parsed.force)
            _print_structured_summary(summary)
            return 0
        if parsed.command == "extract" and parsed.extract_command == "pdf":
            summary = extract_pdf(parsed.source, workers=parsed.workers, force=parsed.force)
            _print_pdf_summary(summary)
            return 0
        if parsed.command == "extract" and parsed.extract_command == "pdf-table":
            summary = extract_pdf_tables(
                parsed.source,
                workers=parsed.workers,
                force=parsed.force,
                ground_truth=_load_ground_truth(parsed.ground_truth),
            )
            _print_pdf_table_summary(summary)
            return 0
        if parsed.command == "extract" and parsed.extract_command == "ocr":
            summary = extract_ocr(parsed.source, workers=parsed.workers, force=parsed.force)
            _print_ocr_summary(summary)
            return 0 if summary.failures == 0 else 0
        if parsed.command == "extract" and parsed.extract_command == "unified":
            summary = extract_unified(parsed.source, workers=parsed.workers, force=parsed.force)
            _print_unified_summary(summary)
            return 0
        if parsed.command == "process":
            summary = process_source(
                parsed.source,
                workers=parsed.workers,
                force=parsed.force,
                drop_exact_duplicates=parsed.drop_exact_duplicates,
            )
            _print_process_summary(summary)
            return 0 if summary.cleaning_failures == 0 else 0
        if parsed.command == "semantic" and parsed.semantic_command == "status":
            status = semantic_status()
            print(f"Provider: {status['provider']}")
            print(f"Model: {status['model']}")
            print(f"LLM_STATUS = {status['llm_status']}")
            print(f"Network calls: {'enabled' if status['network_calls'] else 'disabled'}")
            return 0
        if parsed.command == "semantic" and parsed.semantic_command == "enrich":
            try:
                summary = enrich_catalog(
                    source=parsed.source,
                    asset_id=parsed.asset_id,
                    asset_type=parsed.asset_type,
                    provider_name=parsed.provider,
                    force=parsed.force,
                    limit=parsed.limit,
                )
            except SemanticNotConfigured:
                print("Semantic enrichment is not configured.")
                return 0
            except RealSemanticProviderDisabled as exc:
                print(str(exc))
                return 0
            _print_semantic_summary(summary)
            return 0
        if parsed.command == "benchmark" and parsed.benchmark_command == "scan":
            summary = scan_source(parsed.source, workers=parsed.workers, rehash=parsed.rehash)
            _print_summary(summary)
            print("Benchmark:")
            for key, value in benchmark_metrics(summary).items():
                print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
            return 0 if summary.status == "complete" else 1
        if parsed.command == "benchmark" and parsed.benchmark_command == "structured":
            summary = extract_structured(parsed.source, workers=parsed.workers, force=parsed.force)
            _print_structured_summary(summary)
            print("Benchmark:")
            for key, value in summary.benchmark_metrics().items():
                print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
            return 0
        if parsed.command == "benchmark" and parsed.benchmark_command == "pdf":
            summary = extract_pdf(parsed.source, workers=parsed.workers, force=parsed.force)
            _print_pdf_summary(summary)
            print("Benchmark:")
            for key, value in summary.benchmark_metrics().items():
                print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
            return 0
        if parsed.command == "benchmark" and parsed.benchmark_command == "pdf-table":
            summary = extract_pdf_tables(
                parsed.source,
                workers=parsed.workers,
                force=parsed.force,
                ground_truth=_load_ground_truth(parsed.ground_truth),
            )
            _print_pdf_table_summary(summary)
            print("Benchmark:")
            for key, value in summary.benchmark_metrics().items():
                print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
            return 0
        if parsed.command == "benchmark" and parsed.benchmark_command == "pdf-real":
            result = run_real_pdf_benchmark(
                parsed.source,
                workers=parsed.workers,
                max_samples=parsed.max_samples,
                max_negative_samples=parsed.max_negative_samples,
                force=parsed.force,
            )
            print(f"Source: {result.native_summary.source_root}")
            print(f"Samples: {result.sample_count}")
            print(f"Native-text PDFs: {result.native_summary.native_text_pdfs}")
            print(f"Mixed PDFs: {result.native_summary.mixed_pdfs}")
            print(f"Suspected-scanned PDFs: {result.native_summary.suspected_scanned_pdfs}")
            print(f"Unknown PDFs: {result.native_summary.unknown_pdfs}")
            print(f"Table candidate assets: {result.table_summary.table_assets}")
            print(f"Table candidate failures: {result.table_summary.extraction_failures}")
            print(f"Native reuse: {result.native_summary.reused}")
            print(f"Table reuse: {result.table_summary.reused}")
            print(f"Manifest: {result.manifest_path}")
            print(f"Review CSV: {result.review_path}")
            print(f"Report: {result.report_path}")
            return 0
        if parsed.command == "benchmark" and parsed.benchmark_command == "pdf-consistency":
            result = run_rendered_page_consistency(
                parsed.review_root if parsed.review_root is not None else None,
                max_pages=parsed.max_pages,
                workers=parsed.workers,
                force=parsed.force,
            )
            print("Comparison: rendered native PDF pages treated as simulated scan inputs")
            print("Accuracy claim: none; native output is a weak reference only")
            print(f"Selected pages: {result.selected_pages}")
            print(f"Native candidate pages: {result.native_candidate_pages}")
            print(f"Native negative pages: {result.native_negative_pages}")
            print(f"OCR text assets: {result.ocr_summary.text_assets_produced}")
            print(f"Image table assets: {result.ocr_summary.table_assets_produced}")
            print(f"Table-count-consistent pages: {result.table_count_consistent_pages}")
            print(f"Shape-consistent tables: {result.shape_consistent_tables}")
            print(f"Normalized cell overlap ratio: {result.normalized_cell_overlap_ratio:.4f}")
            print(f"Source unchanged: {result.source_unchanged}")
            print(f"First-pass wall time: {result.first_run_metrics.get('wall clock ms', 'unavailable')} ms")
            print(f"Reuse-pass wall time: {result.reuse_run_metrics.get('wall clock ms', 'unavailable')} ms")
            print(f"Wall time: {result.ocr_summary.wall_time_ms:.2f} ms")
            print(f"Report: {result.report_path}")
            return 0
        if parsed.command == "benchmark" and parsed.benchmark_command == "ocr":
            summary = extract_ocr(parsed.source, workers=parsed.workers, force=parsed.force)
            _print_ocr_summary(summary)
            print("Benchmark:")
            for key, value in summary.benchmark_metrics().items():
                print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
            return 0
        if parsed.command == "benchmark" and parsed.benchmark_command == "cleaning":
            summary = process_source(
                parsed.source,
                workers=parsed.workers,
                force=parsed.force,
                drop_exact_duplicates=parsed.drop_exact_duplicates,
            )
            _print_process_summary(summary)
            print("Benchmark:")
            for key, value in summary.benchmark_metrics().items():
                print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
            return 0
        if parsed.command == "catalog":
            registry = Registry.open()
            try:
                source_arg = getattr(parsed, "source", None)
                source = canonical_source_root(source_arg) if source_arg is not None else None
                if parsed.catalog_command == "summary":
                    _print_catalog_summary(registry.catalog_summary(source))
                elif parsed.catalog_command == "list":
                    rows = registry.list_catalog_assets(
                        source_root=source,
                        asset_type=parsed.asset_type,
                        quality_status=parsed.quality_status,
                        source_format=parsed.source_format,
                        limit=parsed.limit,
                    )
                    print(json.dumps(rows, ensure_ascii=False, default=str, indent=2))
                else:
                    details = registry.catalog_asset_details(parsed.asset_id)
                    if details is None:
                        print(f"ERROR: catalog asset not found: {parsed.asset_id}", file=sys.stderr)
                        return 1
                    print(
                        json.dumps(
                            _catalog_show_payload(details, rows=parsed.rows, chars=parsed.chars),
                            ensure_ascii=False,
                            default=str,
                            indent=2,
                        )
                    )
                return 0
            finally:
                registry.close()
        if parsed.command == "search":
            service = SearchService()
            response = service.search(
                SearchQuery(
                    parsed.query,
                    asset_type=parsed.asset_type,
                    source_format=parsed.source_format,
                    quality_status=parsed.quality_status,
                    limit=parsed.limit,
                    match=parsed.match,
                )
            )
            _print_search_results(response)
            return 0
        if parsed.command == "benchmark" and parsed.benchmark_command == "search":
            service = SearchService()
            query_values = tuple(parsed.queries) if parsed.queries else ("data", "pdf", "表", "")
            benchmark = run_search_benchmark(service, queries=query_values, source_format=parsed.source_format)
            print("Search benchmark:")
            for key, value in benchmark.as_dict().items():
                print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
            return 0
        if parsed.command == "benchmark" and parsed.benchmark_command == "sql":
            benchmark = run_sql_benchmark(SqlQueryService())
            print("SQL benchmark:")
            for key, value in benchmark.as_dict().items():
                print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
            return 0
        if parsed.command == "registry":
            registry = Registry.open()
            try:
                source = canonical_source_root(parsed.source) if parsed.source is not None else None
                if parsed.registry_command == "summary":
                    result = registry.latest_run(source)
                    if result is not None:
                        result["catalog"] = registry.catalog_summary(source)
                    print(json.dumps(result or {}, ensure_ascii=False, default=str, indent=2))
                else:
                    rows = registry.list_files(source, parsed.state, parsed.limit)
                    print(json.dumps(rows, ensure_ascii=False, default=str, indent=2))
                return 0
            finally:
                registry.close()
        parser.print_help()
        return 0
    except (
        ScanError,
        StructuredExtractionError,
        PDFExtractionError,
        PDFTableExtractionError,
        OCRExtractionError,
        UnifiedExtractionError,
        RegistryError,
        ValueError,
        OSError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by the launcher
    raise SystemExit(main())
