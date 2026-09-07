"""Focused M2 contracts for source-first reading and bounded local analysis."""

from __future__ import annotations

import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from dongjian.api.app import ApiError, BackendApp
from dongjian.discovery import discover
from dongjian.extract.docx import _table_presentation
from dongjian.clean import process_source
from dongjian.search import SearchQuery, SearchService
from dongjian.services.catalog import CatalogService


def _process(source: Path, workspace: Path) -> CatalogService:
    process_source(
        source,
        workers=1,
        force=True,
        registry_path=workspace / "state" / "registry.duckdb",
        workspace_root=workspace,
    )
    return CatalogService(
        registry_path=workspace / "state" / "registry.duckdb",
        workspace_root=workspace,
    )


def test_exploded_ooxml_is_ignored_while_nested_normal_files_remain(tmp_path: Path) -> None:
    source = tmp_path / "project"
    (source / "nested").mkdir(parents=True)
    (source / "research.docx").write_bytes(b"source")
    (source / "dataset.csv").write_text("name,value\nalpha,1\n", encoding="utf-8")
    (source / "nested" / "notes.txt").write_text("normal nested file", encoding="utf-8")
    exploded = source / "template_unpacked"
    (exploded / "_rels").mkdir(parents=True)
    (exploded / "word" / "media").mkdir(parents=True)
    (exploded / "[Content_Types].xml").write_text("<Types />", encoding="utf-8")
    (exploded / "word" / "document.xml").write_text("<document />", encoding="utf-8")
    (exploded / "word" / "media" / "image1.png").write_bytes(b"package media")

    _root, files, issues = discover(source)

    assert [item.relative_path for item in files] == ["dataset.csv", "nested/notes.txt", "research.docx"]
    assert any(issue.code == "package_internal" and not issue.is_error for issue in issues)


def test_file_search_returns_every_occurrence_with_true_total(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "repeated.txt").write_text(" ".join(["needle"] * 10), encoding="utf-8")
    workspace = tmp_path / "workspace"
    catalog = _process(source, workspace)
    file_id = catalog.list_files()["items"][0]["fileId"]

    service = SearchService(registry_path=workspace / "state" / "registry.duckdb")
    response = service.search_file_occurrences(file_id, SearchQuery("needle", limit=3))

    assert response.total_occurrences == 10
    assert len(response.results) == 3
    assert len({item.start_offset for item in response.results}) == 3
    later = service.search_file_occurrences(file_id, SearchQuery("needle", limit=100, offset=3))
    assert later.total_occurrences == 10
    assert len(later.results) == 7


def test_csv_source_header_is_used_by_preview_and_report_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "dataset.csv").write_text("Sample Name,Value\nalpha,4\nbeta,7\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    catalog = _process(source, workspace)
    file_id = catalog.list_files()["items"][0]["fileId"]
    content = catalog.file_content(file_id)
    assert content is not None
    tables = [
        block
        for section in content["sections"]
        for block in section["blocks"]
        if block.get("type") == "table"
    ]
    assert tables and tables[0]["preview"]["presentationColumns"] == ["Sample Name", "Value"]

    app = BackendApp(project_root=tmp_path, workspace_root=workspace, registry_path=workspace / "state" / "registry.duckdb")
    try:
        response = app.handle_api(
            "POST",
            "/api/v1/reports",
            {},
            json.dumps({"fileIds": [file_id], "reportType": "analysis"}).encode("utf-8"),
        )
        assert response.status == 202
        report_id = response.payload["reportId"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not app.reports.path_for(report_id).exists():
            time.sleep(0.05)
        record = app.reports.read(report_id)
        assert record is not None
        assert record["report_type"] == "analysis"
        assert record["executed_safe_sql"]
        assert "Sample Name" in json.dumps(record, ensure_ascii=False)
        assert "source_col_0001" not in json.dumps(record, ensure_ascii=False)
    finally:
        app.close(timeout=3)


def test_docx_source_presentation_keeps_occupancy_and_merges() -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = f"""
    <w:tbl xmlns:w="{namespace}">
      <w:tblGrid><w:gridCol w:w="1000"/><w:gridCol w:w="1000"/><w:gridCol w:w="1000"/></w:tblGrid>
      <w:tr>
        <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>wide</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>right</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>merged</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>middle</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>right</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:tcPr><w:vMerge w:val="continue"/></w:tcPr><w:p/></w:tc>
        <w:tc><w:p><w:r><w:t>next</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>last</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    """

    presentation = _table_presentation(ET.fromstring(xml))

    assert presentation["grid_widths_twips"] == [1000, 1000, 1000]
    assert presentation["occupancy"] == [[0, 0, 1], [2, 3, 4], [2, 5, 6]]
    assert presentation["cells"][2]["row_span"] == 2
    assert presentation["cells"][0]["column_span"] == 2


def test_report_requires_explicit_selection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    app = BackendApp(project_root=tmp_path, workspace_root=workspace, registry_path=workspace / "state" / "registry.duckdb")
    try:
        try:
            app.handle_api("POST", "/api/v1/reports", {}, json.dumps({"reportType": "overview"}).encode("utf-8"))
        except ApiError as exc:
            assert exc.code == "REPORT_INPUT_INVALID"
        else:
            raise AssertionError("empty report selection must be rejected")
    finally:
        app.close(timeout=2)
