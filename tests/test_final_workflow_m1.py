"""Focused contracts for the uncommitted Final Workflow M1 slice."""

from __future__ import annotations

from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

import pytest
from PIL import Image

from dongjian.clean import process_source
from dongjian.extract.docx import _table_presentation
from dongjian.extract.ocr.runner import MAX_OCR_PAGE_PIXELS, _guard_page_pixels
from dongjian.registry import Registry
from dongjian.semantic.models import SemanticResponse
from dongjian.services.catalog import CatalogService
from dongjian.services.file_insight import FileInsightError, FileInsightQueueStore, FileInsightService
from dongjian.search import SearchQuery, SearchService

from tests.pdf_factory import write_pdf
from tests.xlsx_factory import write_xlsx


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    return source, workspace


def _process(source: Path, workspace: Path) -> CatalogService:
    process_source(source, workers=1, force=True, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    return CatalogService(registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)


def test_pdf_locator_returns_one_page_and_direct_default_remains_compatible(tmp_path: Path) -> None:
    source, workspace = _paths(tmp_path)
    write_pdf(source / "two-pages.pdf", [{"texts": [(72, 72, "page one")]}, {"texts": [(72, 72, "page two")]}])
    catalog = _process(source, workspace)
    file_id = catalog.list_files(limit=10)["items"][0]["fileId"]

    page = catalog.file_content(file_id, page=2)
    assert page is not None
    assert page["navigation"] == {"kind": "page", "current": 2, "total": 2}
    assert [item["pageNumber"] for item in page["sections"]] == [2]
    assert "page two" in str(page)

    legacy = catalog.file_content(file_id)
    assert legacy is not None
    assert len(legacy["sections"]) == 2
    assert legacy["navigation"]["current"] is None


def test_xlsx_presentation_keeps_geometry_metadata_and_sheet_locator(tmp_path: Path) -> None:
    source, workspace = _paths(tmp_path)
    path = source / "geometry.xlsx"
    write_xlsx(path, [("Sheet A", [["Merged title", None], ["name", "value"]], None), ("Sheet B", [["other"]], None)])
    with zipfile.ZipFile(path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    xml = entries["xl/worksheets/sheet1.xml"].decode("utf-8").replace(
        "</worksheet>", '<mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells></worksheet>'
    )
    entries["xl/worksheets/sheet1.xml"] = xml.encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)

    catalog = _process(source, workspace)
    file_id = catalog.list_files(limit=10)["items"][0]["fileId"]
    content = catalog.file_content(file_id, sheet="Sheet A")
    assert content is not None
    assert content["navigation"]["items"] == ["Sheet A", "Sheet B"]
    presentation = content["sections"][0]["blocks"][0]["preview"]["presentation"]
    assert "A1:B1" in presentation["merged_ranges"]
    assert presentation["cells"]


def test_source_visual_crop_is_available_for_pdf_and_png(tmp_path: Path) -> None:
    source, workspace = _paths(tmp_path)
    write_pdf(source / "table.pdf", [{"rectangles": [(40, 40, 260, 180)]}])
    Image.new("RGB", (320, 220), "white").save(source / "table.png")
    catalog = _process(source, workspace)
    files = {item["relativePath"]: item["fileId"] for item in catalog.list_files(limit=10)["items"]}

    pdf_crop, pdf_type = catalog.file_preview(files["table.pdf"], page=1, bbox=(40, 40, 260, 180))
    image_crop, image_type = catalog.file_preview(files["table.png"], bbox=(20, 20, 180, 120))
    assert pdf_type == "image/png" and pdf_crop.startswith(b"\x89PNG")
    assert image_type == "image/png" and image_crop.startswith(b"\x89PNG")


def test_docx_presentation_preserves_grid_span_vertical_merge_and_form_layout() -> None:
    xml = """
    <w:tbl xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:tblGrid><w:gridCol w:w="1200"/><w:gridCol w:w="1800"/><w:gridCol w:w="900"/></w:tblGrid>
      <w:tr>
        <w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>Label</w:t></w:r></w:p></w:tc>
        <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:tcPr><w:vMerge w:val="continue"/></w:tcPr></w:tc>
        <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Next value</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    """
    presentation = _table_presentation(ET.fromstring(xml))
    assert presentation["grid_widths_twips"] == [1200, 1800, 900]
    assert presentation["column_count"] == 3
    assert presentation["form_like"] is True
    assert presentation["cells"][0]["row_span"] == 2
    assert presentation["cells"][1]["column_span"] == 2


def test_ready_local_callback_can_read_first_file_while_second_is_pending(tmp_path: Path) -> None:
    source, workspace = _paths(tmp_path)
    (source / "first.txt").write_text("first file", encoding="utf-8")
    (source / "second.txt").write_text("second file", encoding="utf-8")
    registry_path = workspace / "state" / "registry.duckdb"
    visible: list[int] = []

    def progress(stage: str, _value: float, **_metadata: object) -> None:
        if stage == "local_ready":
            catalog = CatalogService(registry_path=registry_path, workspace_root=workspace)
            visible.append(catalog.list_files(limit=10)["pagination"]["total"])

    process_source(source, workers=1, force=True, registry_path=registry_path, workspace_root=workspace, progress_callback=progress)
    assert visible and visible[0] >= 1


def test_file_scoped_search_does_not_return_chunks_from_other_files(tmp_path: Path) -> None:
    source, workspace = _paths(tmp_path)
    (source / "first.txt").write_text("shared needle", encoding="utf-8")
    (source / "second.txt").write_text("shared needle", encoding="utf-8")
    catalog = _process(source, workspace)
    files = catalog.list_files(limit=10)["items"]
    first_id = next(item["fileId"] for item in files if item["relativePath"] == "first.txt")
    results = SearchService(registry_path=workspace / "state" / "registry.duckdb").search(SearchQuery("needle", file_id=first_id, limit=20))
    assert results.results
    assert {item.as_dict()["fileId"] for item in results.results} == {first_id}


class _FakeInsightProvider:
    model = "fake-file-insight"

    def __init__(self, *, invalid_evidence: bool = False) -> None:
        self.calls = []
        self.invalid_evidence = invalid_evidence

    def generate(self, request):
        self.calls.append(request)
        refs = [{"asset_id": "not-an-asset"}] if self.invalid_evidence else []
        return SemanticResponse(
            payload={
                "file_type": "txt",
                "document_kind": "note",
                "summary": "这是有依据的本地文件摘要。",
                "important_topics": ["研究"],
                "key_entities_or_fields": [],
                "important_metrics": [],
                "table_summaries": [],
                "date_range": "",
                "quality_notes": [],
                "analysis_suggestions": [],
                "evidence_refs": refs,
                "confidence": 0.8,
            },
            provider="fake",
            model=self.model,
        )


def test_file_insight_is_one_call_bounded_and_cache_reuses_zero_calls(tmp_path: Path) -> None:
    source, workspace = _paths(tmp_path)
    (source / "note.txt").write_text("research context " * 2_000, encoding="utf-8")
    catalog = _process(source, workspace)
    file_id = catalog.list_files(limit=10)["items"][0]["fileId"]
    service = FileInsightService(catalog=catalog, workspace_root=workspace)
    provider = _FakeInsightProvider()

    first = service.enrich(file_id, provider)
    second = service.enrich(file_id, provider)
    assert first["provider_calls"] == 1
    assert second["provider_calls"] == 0
    assert len(provider.calls) == 1
    assert provider.calls[0].payload_bytes <= 96 * 1024


def test_file_insight_rejects_ungrounded_evidence_and_persists_failure(tmp_path: Path) -> None:
    source, workspace = _paths(tmp_path)
    (source / "note.txt").write_text("grounded text", encoding="utf-8")
    catalog = _process(source, workspace)
    file_id = catalog.list_files(limit=10)["items"][0]["fileId"]
    service = FileInsightService(catalog=catalog, workspace_root=workspace)

    with pytest.raises(FileInsightError, match="evidence"):
        service.enrich(file_id, _FakeInsightProvider(invalid_evidence=True))
    assert service.status(file_id)["status"] == "failed"


def test_file_insight_queue_manifest_survives_reopen_and_clears_terminal_work(tmp_path: Path) -> None:
    first = FileInsightQueueStore(tmp_path / "workspace")
    first.enqueue("file-1", "a" * 64)
    reopened = FileInsightQueueStore(tmp_path / "workspace")
    assert reopened.pending()[0]["file_id"] == "file-1"
    reopened.mark_running("file-1")
    assert reopened.pending()[0]["status"] == "running"
    reopened.mark_failed("file-1")
    assert reopened.pending() == []
    reopened.enqueue("file-1", "a" * 64)
    reopened.mark_completed("file-1")
    assert reopened.pending() == []


def test_ocr_guard_is_explicit_and_bounded() -> None:
    with pytest.raises(RuntimeError, match="memory guard"):
        _guard_page_pixels(MAX_OCR_PAGE_PIXELS + 1, 1)
