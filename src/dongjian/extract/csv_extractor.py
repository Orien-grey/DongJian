"""Strict, streaming CSV/TSV extraction."""

from __future__ import annotations

import codecs
import csv
from importlib.metadata import version
from pathlib import Path
import time
from uuid import uuid4

from .artifacts import make_issue, publish_csv_table
from .models import FileExtractionResult, StructuredSource


EXTRACTOR_NAME = "polars-delimited"
EXTRACTOR_VERSION = version("polars")


class DelimitedStructureError(ValueError):
    pass


def _decodes_strictly(path: Path, encoding: str) -> bool:
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                decoder.decode(chunk)
        decoder.decode(b"", final=True)
        return True
    except UnicodeDecodeError:
        return False


def choose_encoding(path: Path) -> str:
    with path.open("rb") as handle:
        prefix = handle.read(3)
    if prefix == codecs.BOM_UTF8:
        return "utf-8-sig"
    if _decodes_strictly(path, "utf-8"):
        return "utf-8"
    if _decodes_strictly(path, "gb18030"):
        return "gb18030"
    raise UnicodeError("file is neither strict UTF-8 nor conservative GB18030/Windows Chinese text")


def _transcode_to_utf8(
    source: Path, encoding: str, extraction_run_id: str, workspace_root: Path
) -> tuple[Path, bool]:
    if encoding == "utf-8":
        return source, False
    target_dir = workspace_root / "staging" / "structured" / extraction_run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid4().hex}.utf8.csv"
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    with source.open("rb") as reader, target.open("wb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(decoder.decode(chunk).encode("utf-8"))
        writer.write(decoder.decode(b"", final=True).encode("utf-8"))
    return target, True


def validate_records(path: Path, delimiter: str) -> tuple[list[str], int]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter, strict=True)
        try:
            header = next(reader)
        except StopIteration:
            return [], 0
        width = len(header)
        if width == 0:
            raise DelimitedStructureError("header has no columns")
        logical_count = 1
        for logical_index, row in enumerate(reader, start=1):
            logical_count += 1
            if len(row) != width:
                raise DelimitedStructureError(
                    f"ragged row at logical record {logical_index}: expected {width} fields, got {len(row)}"
                )
    return header, logical_count


def extract_csv(source: StructuredSource, extraction_run_id: str, extraction_identity: str) -> FileExtractionResult:
    result = FileExtractionResult(
        source=source,
        extraction_run_id=extraction_run_id,
        extraction_identity=extraction_identity,
        extractor=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION,
    )
    utf8_path: Path | None = None
    temporary = False
    started = time.perf_counter_ns()
    extraction_timing_recorded = False
    try:
        encoding = choose_encoding(source.path)
        utf8_path, temporary = _transcode_to_utf8(source.path, encoding, extraction_run_id, source.workspace_root)
        header, logical_count = validate_records(utf8_path, "\t" if source.business_format == "tsv" else ",")
        result.timings.extraction_ms += (time.perf_counter_ns() - started) / 1_000_000
        extraction_timing_recorded = True
        result.warnings.append({"encoding": encoding})
        if logical_count == 0:
            result.status = "partial"
            result.issues.append(
                make_issue(
                    asset_id=f"file:{source.file_id}",
                    issue_type="empty_file",
                    evidence={"source_relative_path": source.relative_path},
                )
            )
            return result
        result.assets.append(
            publish_csv_table(
                source=source,
                result=result,
                utf8_path=utf8_path,
                delimiter="\t" if source.business_format == "tsv" else ",",
                header=header,
                logical_record_count=logical_count,
            )
        )
        result.status = "partial" if result.issues else "successful"
        return result
    except (csv.Error, DelimitedStructureError, UnicodeError, OSError, ValueError) as exc:
        result.status = "failed"
        result.error_category = "malformed_delimited_file"
        result.error_message = str(exc)
        return result
    finally:
        if not extraction_timing_recorded:
            result.timings.extraction_ms += (time.perf_counter_ns() - started) / 1_000_000
        if temporary and utf8_path is not None:
            utf8_path.unlink(missing_ok=True)
