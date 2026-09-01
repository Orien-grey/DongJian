"""Deterministic business-support and extraction planning rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SupportStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class BusinessFormat(str, Enum):
    CSV = "csv"
    TSV = "tsv"
    XLS = "xls"
    XLSX = "xlsx"
    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    DOC = "doc"
    DOCX = "docx"
    PPT = "ppt"
    PPTX = "pptx"
    TXT = "txt"


@dataclass(frozen=True)
class RegistryFileInfo:
    """Only facts already held by the Phase 2 file registry."""

    file_id: str
    detected_type: str
    mime_like_type: str
    observed_extension: str
    routing_class: str


@dataclass(frozen=True)
class ProcessingPlan:
    file_id: str
    support_status: SupportStatus
    business_format: BusinessFormat | None
    attempt_table_extraction: bool
    attempt_text_extraction: bool
    may_require_ocr: bool
    may_require_visual_processing: bool
    reason_code: str

    @property
    def supported(self) -> bool:
        return self.support_status is SupportStatus.SUPPORTED


_UNSUPPORTED_DETECTED_TYPES = {
    "css",
    "html",
    "json",
    "markdown",
    "unknown",
    "xml",
    "zip",
    "zone_identifier",
}
_UNSUPPORTED_TEXT_EXTENSIONS = {".css", ".htm", ".html", ".js", ".json", ".md", ".xml"}


def _supported(
    info: RegistryFileInfo,
    business_format: BusinessFormat,
    *,
    table: bool,
    text: bool,
    ocr: bool = False,
    visual: bool = False,
) -> ProcessingPlan:
    paths = []
    if table:
        paths.append("table")
    if text:
        paths.append("text")
    return ProcessingPlan(
        file_id=info.file_id,
        support_status=SupportStatus.SUPPORTED,
        business_format=business_format,
        attempt_table_extraction=table,
        attempt_text_extraction=text,
        may_require_ocr=ocr,
        may_require_visual_processing=visual,
        reason_code=f"supported_{business_format.value}_{'_and_'.join(paths)}",
    )


def _unsupported(info: RegistryFileInfo, reason_code: str) -> ProcessingPlan:
    return ProcessingPlan(
        file_id=info.file_id,
        support_status=SupportStatus.UNSUPPORTED,
        business_format=None,
        attempt_table_extraction=False,
        attempt_text_extraction=False,
        may_require_ocr=False,
        may_require_visual_processing=False,
        reason_code=reason_code,
    )


def plan_processing(info: RegistryFileInfo) -> ProcessingPlan:
    """Plan independent table and text attempts without invoking an LLM."""

    detected = info.detected_type.casefold().strip()
    extension = info.observed_extension.casefold().strip()

    if detected == "pdf":
        return _supported(info, BusinessFormat.PDF, table=True, text=True, ocr=True, visual=True)
    if detected in {"jpeg", "jpg"}:
        return _supported(info, BusinessFormat.JPEG, table=True, text=True, ocr=True, visual=True)
    if detected == "png":
        return _supported(info, BusinessFormat.PNG, table=True, text=True, ocr=True, visual=True)
    if detected == "xlsx":
        return _supported(info, BusinessFormat.XLSX, table=True, text=False)
    if detected == "docx":
        return _supported(info, BusinessFormat.DOCX, table=True, text=True)
    if detected == "pptx":
        return _supported(info, BusinessFormat.PPTX, table=True, text=True)
    if detected == "delimited_text" and extension in {".csv", ".tsv"}:
        business_format = BusinessFormat.TSV if extension == ".tsv" else BusinessFormat.CSV
        return _supported(info, business_format, table=True, text=False)
    if detected == "ole_compound":
        legacy_format = {
            ".xls": BusinessFormat.XLS,
            ".doc": BusinessFormat.DOC,
            ".ppt": BusinessFormat.PPT,
        }.get(extension)
        if legacy_format is BusinessFormat.XLS:
            return _supported(info, legacy_format, table=True, text=False)
        if legacy_format in {BusinessFormat.DOC, BusinessFormat.PPT}:
            return _supported(info, legacy_format, table=True, text=True)
        return _unsupported(info, "unsupported_ambiguous_ole_container")
    if detected == "plain_text":
        if extension in _UNSUPPORTED_TEXT_EXTENSIONS:
            return _unsupported(info, "unsupported_business_format")
        if extension == ".txt":
            return _supported(info, BusinessFormat.TXT, table=False, text=True)
        return _unsupported(info, "unsupported_plain_text_extension")
    if detected in _UNSUPPORTED_DETECTED_TYPES:
        return _unsupported(info, "unsupported_business_format")
    return _unsupported(info, "unsupported_unknown_type")
