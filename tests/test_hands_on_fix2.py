"""Focused regression coverage for the second hands-on product fixes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import time
import zipfile

import pytest

from dongjian.api.app import ApiError, BackendApp
from dongjian.assets import AssetQualityStatus, SourceKind
from dongjian.clean import process_source
from dongjian.extract.models import StructuredSource
from dongjian.extract.ocr.img2table_adapter import filter_image_table_candidates, publish_image_table
from dongjian.extract.ocr.rapidocr_engine import OCRBlock
from dongjian.registry import Registry
from dongjian.semantic.models import SemanticResponse
from dongjian.semantic.provider import SemanticProviderError
from dongjian.semantic.settings import ProjectAIConfigStore
from dongjian.services.catalog import CatalogService
from tests.pdf_factory import write_pdf
from tests.test_phase5b import _image
from tests.xlsx_factory import write_xlsx


def _process(source: Path, workspace: Path):
    return process_source(
        source,
        workers=1,
        force=True,
        registry_path=workspace / "state" / "registry.duckdb",
        workspace_root=workspace,
    )


def _write_ordered_docx(path: Path) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = (
        '<w:p><w:r><w:t>before table</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Column</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>north</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:p><w:r><w:t>after table</w:t></w:r></w:p>'
    )
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{namespace}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)


def _files_by_name(catalog: CatalogService) -> dict[str, dict]:
    return {item["relativePath"]: item for item in catalog.list_files(limit=100)["items"]}


def test_file_detail_uses_direct_file_id_query_beyond_first_100(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    for index in range(101):
        (source / f"file-{index:03d}.txt").parent.mkdir(parents=True, exist_ok=True)
        (source / f"file-{index:03d}.txt").write_text(f"content {index}", encoding="utf-8")
    _process(source, workspace)
    registry_path = workspace / "state" / "registry.duckdb"
    registry = Registry.open(registry_path)
    try:
        file_id = registry.connection.execute(
            "SELECT file_id FROM files WHERE relative_path='file-100.txt'"
        ).fetchone()[0]
    finally:
        registry.close()
    catalog = CatalogService(registry_path=registry_path, workspace_root=workspace)

    def fail_list_files(*_args, **_kwargs):
        raise AssertionError("file_detail must not build the catalog page")

    monkeypatch.setattr(catalog, "list_files", fail_list_files)
    detail = catalog.file_detail(file_id)
    assert detail is not None
    assert detail["file"]["relativePath"] == "file-100.txt"
    assert detail["timings"]["query_ms"] >= 0
    monkeypatch.undo()
    started = time.perf_counter()
    catalog.file_detail(file_id)
    direct_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    catalog.list_files(limit=100)
    legacy_registry = Registry.open_reader(registry_path)
    try:
        legacy_registry.list_files(limit=100_000)
        legacy_registry.connection.execute(
            "SELECT * FROM catalog_assets WHERE file_id=?", [file_id]
        ).fetchall()
    finally:
        legacy_registry.close()
    legacy_shaped_elapsed = time.perf_counter() - started
    assert direct_elapsed < legacy_shaped_elapsed


def test_file_content_is_mixed_order_and_bounded_for_supported_formats(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir(parents=True)
    (source / "notes.txt").write_text("long text " * 4_000, encoding="utf-8")
    _write_ordered_docx(source / "mixed.docx")
    write_xlsx(
        source / "book.xlsx",
        [
            ("First", [["name", "value"], *[[f"row-{i}", i] for i in range(50)]], None),
            ("Second", [["other", "value"], ["alpha", 7]], None),
        ],
    )
    write_pdf(
        source / "native.pdf",
        [{
            "texts": [
                (72, 72, "PDF page text"),
                (90, 140, "Name"),
                (160, 140, "Value"),
                (90, 170, "north"),
                (160, 170, "12"),
            ],
            "lines": [
                (80, 120, 220, 120),
                (80, 150, 220, 150),
                (80, 180, 220, 180),
                (80, 120, 80, 180),
                (150, 120, 150, 180),
                (220, 120, 220, 180),
            ],
        }],
    )
    _process(source, workspace)
    catalog = CatalogService(registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    files = _files_by_name(catalog)

    text_content = catalog.file_content(files["notes.txt"]["fileId"])
    assert text_content is not None and text_content["sections"]
    text_blocks = text_content["sections"][0]["blocks"]
    assert len(text_blocks[0]["text"]) <= text_content["limits"]["maxTextChars"]
    assert text_content["truncated"] is True

    workbook_content = catalog.file_content(files["book.xlsx"]["fileId"])
    assert workbook_content is not None
    assert [section["kind"] for section in workbook_content["sections"]] == ["sheet", "sheet"]
    assert all(block["previewAvailable"] for section in workbook_content["sections"] for block in section["blocks"])
    assert all(len(block["preview"]["rows"]) <= 20 for section in workbook_content["sections"] for block in section["blocks"])
    assert len(workbook_content["sections"][0]["blocks"][0]["preview"]["columns"]) <= 32

    docx_content = catalog.file_content(files["mixed.docx"]["fileId"])
    assert docx_content is not None
    assert [block["type"] for block in docx_content["sections"][0]["blocks"]] == ["text", "table", "text"]
    assert [block["order"] for block in docx_content["sections"][0]["blocks"]] == [0, 1, 2]

    pdf_content = catalog.file_content(files["native.pdf"]["fileId"])
    assert pdf_content is not None
    assert pdf_content["sections"][0]["kind"] == "page"
    assert "PDF page text" in pdf_content["sections"][0]["blocks"][0]["text"]
    assert any(block["type"] == "table" and block["previewAvailable"] for block in pdf_content["sections"][0]["blocks"])


def test_file_content_table_preview_is_bounded_and_api_is_additive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    write_xlsx(source / "only-table.xlsx", [("Data", [["a", "b"], *[[i, i * 2] for i in range(80)]], None)])
    _process(source, workspace)
    catalog = CatalogService(registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    file_id = catalog.list_files()["items"][0]["fileId"]
    app = BackendApp(project_root=tmp_path, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    try:
        old_detail = app.handle_api("GET", f"/api/v1/files/{file_id}", {}).payload
        content = app.handle_api("GET", f"/api/v1/files/{file_id}/content", {}).payload
    finally:
        app.close(timeout=5)
    assert old_detail["file"]["tableAssets"] == 1
    assert content["sections"][0]["kind"] == "sheet"
    assert content["sections"][0]["blocks"][0]["preview"]["pagination"]["hasNext"] is True
    assert "暂无正文" not in json.dumps(content, ensure_ascii=False)


def test_image_file_content_exposes_ocr_and_candidate_preview(tmp_path: Path) -> None:
    source = tmp_path / "image-source"
    _image(source / "report.png", table=True)
    _process(source, tmp_path / "workspace")
    catalog = CatalogService(
        registry_path=tmp_path / "workspace" / "state" / "registry.duckdb",
        workspace_root=tmp_path / "workspace",
    )
    file_id = catalog.list_files()["items"][0]["fileId"]
    content = catalog.file_content(file_id)
    assert content is not None
    blocks = content["sections"][0]["blocks"]
    assert any(block["type"] == "text" for block in blocks)
    table_blocks = [block for block in blocks if block["type"] == "table"]
    assert table_blocks
    assert all(block["candidate"] and block["candidateStatus"] == "candidate" for block in table_blocks)
    assert all(block["previewAvailable"] for block in table_blocks)


class _Box:
    def __init__(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2


class _Cell:
    def __init__(self, value: object) -> None:
        self.value = value
        self.bbox = None


class _ImageTable:
    def __init__(self, rows: list[list[object]], bbox: _Box | None) -> None:
        self.content = {index: [_Cell(value) for value in row] for index, row in enumerate(rows)}
        self.bbox = bbox


def test_image_candidates_filter_empty_and_high_overlap_duplicates(tmp_path: Path) -> None:
    first = _ImageTable([["A", "B"], ["1", "2"]], _Box(0, 0, 100, 100))
    duplicate = _ImageTable([["A", "B"], ["1", "2"]], _Box(1, 1, 99, 99))
    kept, decisions = filter_image_table_candidates([first, duplicate])
    assert [index for index, _table in kept] == [0]
    assert decisions[0]["reason"] == "high_overlap_high_similarity"
    empty = _ImageTable([["", ""], [None, ""]], _Box(0, 0, 100, 100))
    source = StructuredSource("file", "a" * 64, str(tmp_path), "image.png", "png", 1, 1, tmp_path / "workspace")
    publication = publish_image_table(
        source=source,
        extraction_run_id="run",
        table=empty,
        table_index=0,
        blocks=(),
        image_width=100,
        image_height=100,
        source_kind=SourceKind.IMAGE,
        page_number=None,
    )
    assert publication.asset is None
    assert publication.issues[0].evidence["reason"] == "empty_table_structure"


def test_image_candidate_without_bbox_does_not_receive_fake_coordinates(tmp_path: Path) -> None:
    table = _ImageTable([["A", "B"], ["1", "2"]], None)
    source = StructuredSource("file", "b" * 64, str(tmp_path), "image.png", "png", 1, 1, tmp_path / "workspace")
    publication = publish_image_table(
        source=source,
        extraction_run_id="run",
        table=table,
        table_index=0,
        blocks=(OCRBlock("A", 0.9, ((0, 0), (1, 0), (1, 1), (0, 1))),),
        image_width=100,
        image_height=100,
        source_kind=SourceKind.IMAGE,
        page_number=None,
    )
    assert publication.asset is not None
    metadata = json.loads((tmp_path / "workspace" / publication.asset.metadata_artifact_path).read_text(encoding="utf-8"))
    assert metadata["candidate_bbox"] is None
    assert metadata["img2table_bbox_pixels"] is None


def test_image_candidate_shape_warnings_are_downgrade_signals(tmp_path: Path) -> None:
    single = _ImageTable([["title"]], _Box(0, 0, 100, 100))
    source = StructuredSource("file", "c" * 64, "root", "image.png", "png", 1, 1, tmp_path / "workspace")
    publication = publish_image_table(
        source=source,
        extraction_run_id="run",
        table=single,
        table_index=0,
        blocks=(),
        image_width=100,
        image_height=100,
        source_kind=SourceKind.IMAGE,
        page_number=None,
    )
    assert publication.asset is not None
    assert publication.asset.quality_status == AssetQualityStatus.REVIEW
    assert {issue.issue_type for issue in publication.issues} >= {"suspicious_single_row", "suspicious_single_column"}


class _ProbeProvider:
    name = "probe"
    model = "probe-model"

    def __init__(self, response: SemanticResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or SemanticResponse(payload={"ok": True}, provider="probe", model="probe-model", structured_output_ok=False)
        self.error = error
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def _configured_project(project: Path) -> None:
    ProjectAIConfigStore(project).save(
        base_url="http://127.0.0.1:9/v1",
        api_key="persisted-key",
        model="persisted-model",
        timeout=120,
        vision_enabled=False,
    )


def test_connection_tests_draft_without_persisting_or_echoing_key(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _configured_project(project)
    provider = _ProbeProvider()
    app = BackendApp(project_root=project, semantic_provider=provider)
    try:
        response = app.handle_api(
            "POST",
            "/api/v1/settings/ai/test",
            {},
            json.dumps({"baseUrl": "http://draft.test/v1", "apiKey": "draft-secret", "model": "draft-model", "timeout": 7}).encode(),
        )
    finally:
        app.close(timeout=5)
    saved = ProjectAIConfigStore(project).read()
    assert response.payload["connectionOk"] is True
    assert response.payload["structuredOutputOk"] is False
    assert response.payload["settings"]["model"] == "persisted-model"
    assert "draft-secret" not in json.dumps(response.payload)
    assert saved.api_key == "persisted-key"
    assert provider.requests and provider.requests[0].structured_output_required is False
    assert provider.requests[0].reference_data == {"test": "DongJian connection check"}


def test_connection_failure_preserves_draft_and_sanitizes_diagnostic(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _configured_project(project)
    provider = _ProbeProvider(error=SemanticProviderError("provider failure", code="http_401", diagnostic="<b>Authorization: Bearer draft-secret</b>"))
    app = BackendApp(project_root=project, semantic_provider=provider)
    try:
        with pytest.raises(ApiError) as raised:
            app.handle_api(
                "POST",
                "/api/v1/settings/ai/test",
                {},
                json.dumps({"baseUrl": "http://draft.test/v1", "apiKey": "draft-secret", "model": "draft-model"}).encode(),
            )
    finally:
        app.close(timeout=5)
    assert raised.value.code == "AUTH_FAILED"
    assert raised.value.category == "AUTH_FAILED"
    assert "draft-secret" not in (raised.value.diagnostic or "")
    assert "<b>" not in (raised.value.diagnostic or "")
    assert ProjectAIConfigStore(project).read().model == "persisted-model"


def test_connection_probe_uses_one_attempt_and_bounded_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    _configured_project(project)
    provider = _ProbeProvider()
    captured = {}

    def factory(_name, config, *, allow_real_provider=False):
        captured["config"] = config
        return provider

    monkeypatch.setattr("dongjian.api.app.provider_for_name", factory)
    app = BackendApp(project_root=project)
    try:
        app.handle_api("POST", "/api/v1/settings/ai/test", {}, b"{}")
    finally:
        app.close(timeout=5)
    assert captured["config"].max_retries == 0
    assert captured["config"].timeout_seconds == 20
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    ("provider_code", "category"),
    [("http_400", "BAD_REQUEST"), ("http_401", "AUTH_FAILED"), ("http_403", "AUTH_FAILED"), ("http_404", "ENDPOINT_OR_MODEL_NOT_FOUND"), ("http_429", "RATE_LIMITED"), ("timeout", "TIMEOUT"), ("connection_error", "CONNECTION_FAILED"), ("malformed_json", "INVALID_RESPONSE")],
)
def test_connection_diagnostic_categories_are_provider_neutral(tmp_path: Path, provider_code: str, category: str) -> None:
    project = tmp_path / "project"
    _configured_project(project)
    app = BackendApp(project_root=project, semantic_provider=_ProbeProvider(error=SemanticProviderError("failure", code=provider_code)))
    try:
        with pytest.raises(ApiError) as raised:
            app.handle_api("POST", "/api/v1/settings/ai/test", {}, b"{}")
    finally:
        app.close(timeout=5)
    assert raised.value.category == category


def test_project_reset_cancels_active_task_then_clears_generated_state_and_preserves_sources(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = project / "workspace"
    source = workspace / "input" / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("source evidence", encoding="utf-8")
    config = project / "config" / "llm.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"base_url": "", "api_key": "", "model": ""}), encoding="utf-8")
    legacy_settings = workspace / "state" / "llm-settings.json"
    legacy_settings.parent.mkdir(parents=True, exist_ok=True)
    legacy_settings.write_text('{"version": 1, "encryptedApiKey": "preserve"}', encoding="utf-8")
    (workspace / "artifacts" / "analysis").mkdir(parents=True)
    (workspace / "artifacts" / "analysis" / "run.json").write_text("generated", encoding="utf-8")
    (project / "cache" / "temp").mkdir(parents=True)
    (project / "cache" / "temp" / "cache.bin").write_bytes(b"generated")
    app = BackendApp(project_root=project, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    try:
        app.tasks.list = lambda limit=50: [SimpleNamespace(task_id="active", status="running", task_type="process")]
        response = app.handle_api("POST", "/api/v1/workspace/reset", {}, json.dumps({"confirmation": "清空"}).encode())
        assert response.status == 200
        assert source.is_file()
    finally:
        app.close(timeout=5)
    assert response.payload["reset"] is True
    assert source.read_text(encoding="utf-8") == "source evidence"
    assert config.is_file()
    assert legacy_settings.is_file()
    assert not (workspace / "artifacts" / "analysis" / "run.json").exists()
    assert not (project / "cache" / "temp" / "cache.bin").exists()
    registry = Registry.open(workspace / "state" / "registry.duckdb")
    try:
        assert registry.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert registry.connection.execute("SELECT COUNT(*) FROM table_assets").fetchone()[0] == 0
    finally:
        registry.close()


def test_frontend_has_file_content_error_draft_reset_and_sticky_contracts() -> None:
    root = Path(__file__).parents[1]
    app = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    api = (root / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    styles = (root / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
    assert "AbortController" in app and 'fileDetailState === "error"' in app
    assert "FileContentTableBlock" in app and "连接测试只验证了当前草稿" in app
    assert "resetWorkspace" in api and "/api/v1/files/${encodeURIComponent(fileId)}/content" in api
    assert "position: sticky" in styles and "top: 0" in styles
