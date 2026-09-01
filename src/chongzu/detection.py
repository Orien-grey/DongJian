"""Fast, conservative file type detection without external binaries."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .types import DetectionResult


MAX_HEADER_BYTES = 64 * 1024
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
XML_TAG_RE = re.compile(r"^<[A-Za-z_][\w:.-]*(?:\s|/?>)")


def observed_extension(path: Path) -> str:
    """Return the lower-case observed suffix, without treating it as truth."""

    return path.suffix.lower()


def _unknown(*, evidence: str | None = None, error_code: str | None = None, error_message: str | None = None) -> DetectionResult:
    return DetectionResult(
        detected_type="unknown",
        mime_like_type="application/octet-stream",
        method="unknown",
        confidence="low",
        routing_class="unknown",
        evidence=evidence,
        error_code=error_code,
        error_message=error_message,
    )


def _text_readable(sample: bytes) -> str | None:
    if not sample or b"\x00" in sample:
        return None
    try:
        text = sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if not text:
        return ""
    printable = sum(1 for char in text if char in "\r\n\t" or char.isprintable())
    if printable / len(text) < 0.85:
        return None
    return text


def _text_detection(path: Path, extension: str, sample: bytes) -> DetectionResult | None:
    text = _text_readable(sample)
    if text is None:
        return None
    stripped = text.lstrip("\ufeff \t\r\n")
    lower = stripped.lower()
    if lower.startswith(("<!doctype html", "<html", "<head", "<body")) or "<html" in lower[:4096]:
        return DetectionResult("html", "text/html", "text-signature", "medium", "web_asset")
    if lower.startswith("<?xml") or (extension == ".xml" and XML_TAG_RE.match(stripped)):
        return DetectionResult("xml", "application/xml", "text-signature", "medium", "structured")
    if extension == ".css" and "{" in stripped and "}" in stripped:
        return DetectionResult("css", "text/css", "extension+text-heuristic", "medium", "web_asset")
    if extension == ".json" and stripped.startswith(("{", "[")):
        return DetectionResult("json", "application/json", "extension+text-heuristic", "medium", "structured")
    if extension in (".csv", ".tsv"):
        mime = "text/tab-separated-values" if extension == ".tsv" else "text/csv"
        return DetectionResult("delimited_text", mime, "extension+text-heuristic", "medium", "structured")
    if extension in (".md", ".markdown"):
        return DetectionResult("markdown", "text/markdown", "extension+text-heuristic", "low", "document")
    return DetectionResult("plain_text", "text/plain", "text-heuristic", "low", "document")


def _zip_detection(path: Path) -> DetectionResult:
    try:
        # ZipFile reads the central directory; it does not extract members.
        with zipfile.ZipFile(path) as archive:
            names = {name.replace("\\", "/") for name in archive.namelist()}
    except zipfile.BadZipFile as exc:
        return DetectionResult(
            "zip",
            "application/zip",
            "magic+zip-corrupt",
            "medium",
            "archive",
            evidence="corrupted_zip",
            error_code="corrupted_zip",
            error_message=str(exc),
        )
    except (OSError, RuntimeError) as exc:
        return DetectionResult(
            "zip",
            "application/zip",
            "magic+zip-error",
            "low",
            "archive",
            error_code="zip_inspection_error",
            error_message=str(exc),
        )

    folded_names = {name.casefold() for name in names}
    has_content_types = "[content_types].xml" in folded_names
    if has_content_types and any(name.casefold().startswith("xl/") for name in names):
        return DetectionResult(
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "magic+zip-container",
            "high",
            "structured",
        )
    if has_content_types and any(name.casefold().startswith("word/") for name in names):
        return DetectionResult(
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "magic+zip-container",
            "high",
            "document",
        )
    if has_content_types and any(name.casefold().startswith("ppt/") for name in names):
        return DetectionResult(
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "magic+zip-container",
            "high",
            "document",
        )
    return DetectionResult("zip", "application/zip", "magic+zip-container", "high", "archive")


def detect_file(path: Path, extension: str | None = None) -> DetectionResult:
    """Detect a file from its name, header, and (for ZIP) central directory."""

    extension = observed_extension(path) if extension is None else extension.lower()
    if path.name.casefold().endswith("zone.identifier"):
        return DetectionResult("zone_identifier", "text/plain", "filename", "high", "metadata")

    try:
        with path.open("rb") as handle:
            sample = handle.read(MAX_HEADER_BYTES)
    except OSError as exc:
        return _unknown(error_code="detection_error", error_message=str(exc))

    if sample.startswith(b"%PDF-"):
        return DetectionResult("pdf", "application/pdf", "magic", "high", "document")
    if sample.startswith(JPEG_SIGNATURE):
        return DetectionResult("jpeg", "image/jpeg", "magic", "high", "image")
    if sample.startswith(PNG_SIGNATURE):
        return DetectionResult("png", "image/png", "magic", "high", "image")
    if sample.startswith(OLE_SIGNATURE):
        return DetectionResult("ole_compound", "application/x-ole-storage", "magic", "high", "document")
    if sample.startswith(ZIP_SIGNATURES):
        return _zip_detection(path)

    text_result = _text_detection(path, extension, sample)
    if text_result is not None:
        return text_result
    return _unknown()
