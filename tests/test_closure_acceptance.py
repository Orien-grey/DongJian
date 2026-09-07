"""Targeted acceptance fixtures for exact file search and offline reports."""

from __future__ import annotations

import json
from pathlib import Path
import time
from xml.sax.saxutils import escape
import zipfile

from dongjian.api.app import BackendApp
from dongjian.clean import process_source
from dongjian.semantic.provider import SemanticProviderError
from dongjian.services.catalog import CatalogService
from dongjian.services.report import ReportComposer
from dongjian.search import SearchQuery, SearchService
from tests.pdf_factory import write_pdf
from tests.xlsx_factory import write_xlsx


def _process(source: Path, workspace: Path) -> CatalogService:
    registry_path = workspace / "state" / "registry.duckdb"
    process_source(source, workers=1, force=True, registry_path=registry_path, workspace_root=workspace)
    return CatalogService(registry_path=registry_path, workspace_root=workspace)


def _files(catalog: CatalogService) -> dict[str, dict[str, object]]:
    return {str(item["displayName"]): item for item in catalog.list_files(limit=100)["items"]}


def _write_three_match_docx(path: Path) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paragraphs = ["团徽 A", "团徽 B", "团徽 C"]
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", relationships)
        package.writestr("word/document.xml", document)


def _text_block(content: dict[str, object], asset_id: str) -> str:
    for section in content.get("sections", []):
        if not isinstance(section, dict):
            continue
        for block in section.get("blocks", []):
            if isinstance(block, dict) and block.get("assetId") == asset_id and block.get("type") == "text":
                return str(block.get("text") or "")
    raise AssertionError(asset_id)


def test_exact_file_search_uses_final_presentation_and_trusted_cells(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    write_pdf(
        source / "search.pdf",
        [
            {"fontname": "china-s", "texts": [(72, 72, "爱好：摄影")]},
            {"fontname": "china-s", "texts": [(72, 72, "自学：机器学习")]},
        ],
    )
    _write_three_match_docx(source / "search.docx")
    write_xlsx(
        source / "search.xlsx",
        [
            ("第一表", [["项目", "说明"], ["A", "碳减排方案"], ["B", "普通内容"]], None),
            ("第二表", [["项目", "说明"], ["C", "碳减排指标"], ["D", "碳减排"]], None),
        ],
    )
    (source / "search.txt").write_text("重复词一\n重复词二\n重复词三\n", encoding="utf-8")
    catalog = _process(source, workspace)
    files = _files(catalog)
    search = SearchService(registry_path=workspace / "state" / "registry.duckdb")

    pdf_id = str(files["search.pdf"]["fileId"])
    pdf_result = search.search_file_occurrences(pdf_id, SearchQuery(query="爱好", limit=20))
    assert pdf_result.total_occurrences == 1
    assert pdf_result.results[0].page == 1
    assert pdf_result.results[0].snippet.find("爱好") >= 0
    assert "自学" not in pdf_result.results[0].snippet
    assert _text_block(catalog.file_content(pdf_id), pdf_result.results[0].asset_id)[pdf_result.results[0].start_offset:pdf_result.results[0].end_offset] == "爱好"

    docx_id = str(files["search.docx"]["fileId"])
    docx_result = search.search_file_occurrences(docx_id, SearchQuery(query="团徽", limit=20))
    assert docx_result.total_occurrences == 3
    assert len({item.start_offset for item in docx_result.results}) == 3
    docx_content = catalog.file_content(docx_id)
    assert all(
        _text_block(docx_content, item.asset_id)[item.start_offset:item.end_offset] == "团徽"
        for item in docx_result.results
    )

    xlsx_id = str(files["search.xlsx"]["fileId"])
    xlsx_result = search.search_file_occurrences(xlsx_id, SearchQuery(query="碳减排", limit=20))
    assert xlsx_result.total_occurrences == 3
    assert {item.sheet for item in xlsx_result.results} == {"第一表", "第二表"}
    assert all(item.row is not None and item.column is not None for item in xlsx_result.results)
    assert all("碳减排" in str(item.cell_value) for item in xlsx_result.results)

    txt_id = str(files["search.txt"]["fileId"])
    txt_result = search.search_file_occurrences(txt_id, SearchQuery(query="重复词", limit=20))
    assert txt_result.total_occurrences == 3
    assert len({item.start_offset for item in txt_result.results}) == 3
    assert all(_text_block(catalog.file_content(txt_id), item.asset_id)[item.start_offset:item.end_offset] == "重复词" for item in txt_result.results)


class _TimeoutReportProvider:
    name = "synthetic-timeout"
    model = "synthetic-timeout-v1"

    def generate(self, _request: object) -> object:
        raise SemanticProviderError("synthetic timeout", code="timeout", retryable=True)


def _wait_terminal(app: BackendApp, task_id: str, timeout: float = 8.0) -> object:
    deadline = time.monotonic() + timeout
    task = None
    while time.monotonic() < deadline:
        task = app.tasks.get(task_id)
        if task is not None and task.status not in {"queued", "running", "cancelling"}:
            return task
        time.sleep(0.02)
    raise AssertionError(f"report task did not finish: {task_id}, task={task}")


def test_offline_data_report_contains_deterministic_statistics_and_timeout_reuse(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    (source / "sales_a.csv").write_text(
        "category,amount,note\nA,10,ok\nA,10,ok\nB,,missing\nC,30,ok\n",
        encoding="utf-8",
    )
    (source / "sales_b.csv").write_text(
        "category,amount,region\nA,20,north\nB,25,south\n",
        encoding="utf-8",
    )
    catalog = _process(source, workspace)
    files = _files(catalog)
    app = BackendApp(project_root=tmp_path, workspace_root=workspace, registry_path=workspace / "state" / "registry.duckdb")
    try:
        response = app.handle_api(
            "POST",
            "/api/v1/reports",
            {},
            json.dumps(
                {
                    "fileIds": [str(files["sales_a.csv"]["fileId"]), str(files["sales_b.csv"]["fileId"])],
                    "reportType": "analysis",
                    "title": "Synthetic sales analysis",
                }
            ).encode("utf-8"),
        )
        assert response.status == 202
        task = _wait_terminal(app, str(response.payload["taskId"]))
        assert task.status == "succeeded"
        report = app.reports.read(str(response.payload["reportId"]))
        assert report is not None
        assert report["generation_mode"] == "deterministic_fallback"
        rendered = json.dumps(report["structured_report"], ensure_ascii=False)
        for expected in ("2", "行", "列", "category", "amount", "缺失值", "平均值", "最高频分类值", "重复记录", "共享字段"):
            assert expected in rendered

        run_id = str(report["source_analysis_run_ids"][0])
        timeout_report = ReportComposer(
            app.analysis_runs,
            workspace,
            provider=_TimeoutReportProvider(),
            report_store=app.reports,
            ).compose([run_id], report_id="report_synthetic_timeout", report_type="analysis")
        timeout_rendered = json.dumps(timeout_report["structured_report"], ensure_ascii=False)
        assert timeout_report["generation_mode"] == "deterministic_fallback"
        for expected in ("缺失值", "平均值", "最高频分类值", "重复记录", "共享字段"):
            assert expected in timeout_rendered
    finally:
        app.close(timeout=2)
