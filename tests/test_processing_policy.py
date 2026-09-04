from __future__ import annotations

from pathlib import Path

import pytest

from chongzu.processing_policy import BusinessFormat, RegistryFileInfo, SupportStatus, plan_processing
from chongzu.registry import Registry, canonical_source_root
from chongzu.scan import scan_source


def _info(detected_type: str, extension: str = "") -> RegistryFileInfo:
    return RegistryFileInfo(
        file_id="file-1",
        detected_type=detected_type,
        mime_like_type="application/test",
        observed_extension=extension,
        routing_class="test",
    )


def test_pdf_policy_attempts_table_and_text() -> None:
    plan = plan_processing(_info("pdf", ".pdf"))
    assert plan.supported
    assert plan.business_format is BusinessFormat.PDF
    assert plan.attempt_table_extraction
    assert plan.attempt_text_extraction
    assert plan.may_require_ocr


@pytest.mark.parametrize(("detected_type", "extension"), [("jpeg", ".jpg"), ("png", ".png")])
def test_image_policy_is_dual_extraction_and_visual(detected_type: str, extension: str) -> None:
    plan = plan_processing(_info(detected_type, extension))
    assert plan.attempt_table_extraction
    assert plan.attempt_text_extraction
    assert plan.may_require_ocr
    assert plan.may_require_visual_processing


@pytest.mark.parametrize(("detected_type", "extension"), [("ole_compound", ".xls"), ("xlsx", ".xlsx")])
def test_excel_policy_attempts_table_extraction(detected_type: str, extension: str) -> None:
    plan = plan_processing(_info(detected_type, extension))
    assert plan.supported
    assert plan.attempt_table_extraction
    assert not plan.attempt_text_extraction


def test_txt_policy_attempts_only_text_extraction() -> None:
    plan = plan_processing(_info("plain_text", ".txt"))
    assert plan.supported
    assert not plan.attempt_table_extraction
    assert plan.attempt_text_extraction


@pytest.mark.parametrize(
    ("detected_type", "extension", "business_format", "table", "text"),
        [
            ("delimited_text", ".csv", BusinessFormat.CSV, True, False),
            ("delimited_text", ".tsv", BusinessFormat.TSV, True, False),
            ("docx", ".docx", BusinessFormat.DOCX, True, True),
        ],
)
def test_remaining_supported_format_matrix(
    detected_type: str,
    extension: str,
    business_format: BusinessFormat,
    table: bool,
    text: bool,
) -> None:
    plan = plan_processing(_info(detected_type, extension))
    assert plan.business_format is business_format
    assert plan.attempt_table_extraction is table
    assert plan.attempt_text_extraction is text


@pytest.mark.parametrize("detected_type", ["html", "css", "xml"])
def test_web_and_xml_formats_are_unsupported(detected_type: str) -> None:
    plan = plan_processing(_info(detected_type, f".{detected_type}"))
    assert plan.support_status is SupportStatus.UNSUPPORTED
    assert not plan.attempt_table_extraction
    assert not plan.attempt_text_extraction


def test_unknown_is_unsupported() -> None:
    plan = plan_processing(_info("unknown", ".bin"))
    assert plan.support_status is SupportStatus.UNSUPPORTED


def test_javascript_text_is_unsupported_by_business_extension() -> None:
    plan = plan_processing(_info("plain_text", ".js"))
    assert plan.support_status is SupportStatus.UNSUPPORTED


def test_registry_retains_unsupported_file_and_records_policy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "page.html").write_text("<!doctype html><html><body>text</body></html>", encoding="utf-8")
    (source / "note.txt").write_text("text", encoding="utf-8")
    registry_path = tmp_path / "workspace" / "state" / "registry.duckdb"

    summary = scan_source(source, registry_path=registry_path)
    assert summary.discovered_count == 2

    registry = Registry.open(registry_path)
    try:
        rows = {item["relative_path"]: item for item in registry.list_files(canonical_source_root(source))}
        assert rows["page.html"]["support_status"] == "unsupported"
        assert rows["page.html"]["policy_reason"] == "unsupported_business_format"
        assert rows["note.txt"]["support_status"] == "supported"
        assert rows["note.txt"]["text_candidate"] is True
    finally:
        registry.close()
