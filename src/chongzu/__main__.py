"""Command-line entry point for ``python -m chongzu``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .doctor import main as doctor_main
from .registry import Registry, RegistryError, canonical_source_root
from .scan import ScanError, benchmark_metrics, scan_source
from .extract import PDFExtractionError, StructuredExtractionError, extract_pdf, extract_structured


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

    registry_parser = sub.add_parser("registry", help="inspect the local DuckDB registry")
    registry_sub = registry_parser.add_subparsers(dest="registry_command", required=True)
    summary_parser = registry_sub.add_parser("summary")
    summary_parser.add_argument("--source", type=Path, default=None)
    files_parser = registry_sub.add_parser("files")
    files_parser.add_argument("--source", type=Path, default=None)
    files_parser.add_argument("--state", default=None)
    files_parser.add_argument("--limit", type=int, default=1000)

    benchmark_parser = sub.add_parser("benchmark", help="measure scan throughput")
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


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch diagnostics, registry inspection, and extraction commands."""

    args = list(sys.argv[1:] if argv is None else argv)
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
    except (ScanError, StructuredExtractionError, PDFExtractionError, RegistryError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by the launcher
    raise SystemExit(main())
