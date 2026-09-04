"""Deterministic regression checks for the first hands-on product fixes."""

from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading
import time
import zipfile

import pytest

from chongzu.api.app import ApiError, BackendApp
from chongzu.clean import process_source
from chongzu.processing_policy import BusinessFormat, RegistryFileInfo, SupportStatus, plan_processing
from chongzu.search import SearchQuery, SearchService
from chongzu.semantic.provider import SemanticProviderError
from chongzu.semantic.settings import ProjectAIConfigStore
from chongzu.services.catalog import CatalogService
from tests.pdf_factory import write_pdf


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_docx(path: Path, *, paragraphs: list[str], tables: list[list[list[str]]]) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body: list[str] = []
    for paragraph in paragraphs:
        body.append(f'<w:p><w:r><w:t xml:space="preserve">{paragraph}</w:t></w:r></w:p>')
    for table in tables:
        rows = []
        for row in table:
            cells = "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row)
            rows.append(f"<w:tr>{cells}</w:tr>")
        body.append(f"<w:tbl>{''.join(rows)}</w:tbl>")
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>{"".join(body)}'
        "<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)


def _process(source: Path, workspace: Path):
    return process_source(
        source,
        workers=1,
        force=True,
        registry_path=workspace / "state" / "registry.duckdb",
        workspace_root=workspace,
    )


def test_docx_mixed_content_is_one_file_with_readable_children_and_stable_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    docx = source / "research.docx"
    _write_docx(docx, paragraphs=["DOCX introduction", "DOCX conclusion"], tables=[[['Region', 'Value'], ['Beijing', '12']]])
    before = _sha256(docx)

    summary = _process(source, workspace)

    assert summary.files_supported == 1
    assert summary.table_assets == 1
    assert summary.text_assets == 1
    assert _sha256(docx) == before
    catalog = CatalogService(registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    files = catalog.list_files()
    assert files["pagination"]["total"] == 1
    assert files["items"][0]["format"] == "docx"
    assert files["items"][0]["tableAssets"] == 1
    detail = catalog.file_detail(files["items"][0]["fileId"])
    assert detail is not None
    assert len(detail["pages"]) == 1
    assert "DOCX introduction" in detail["pages"][0]["text"]
    assert detail["tables"][0]["source"]["relativePath"] == "research.docx"
    assert detail["tables"][0]["source"]["sha256"] == before
    assert detail["tables"][0]["sheetName"] == "table-1"
    search = SearchService(registry_path=workspace / "state" / "registry.duckdb").search(
        SearchQuery(query="DOCX introduction", asset_type="text", limit=10)
    )
    assert search.results and search.results[0].source_file == "research.docx"


def test_corrupt_docx_isolated_without_hiding_valid_docx(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    valid = source / "valid.docx"
    broken = source / "broken.docx"
    _write_docx(valid, paragraphs=["valid paragraph"], tables=[])
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not an OOXML package")

    summary = _process(source, workspace)

    assert summary.extraction_failures == 1
    catalog = CatalogService(registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    assert catalog.list_files()["pagination"]["total"] == 2
    from chongzu.registry import Registry

    registry = Registry.open(workspace / "state" / "registry.duckdb")
    try:
        runs = registry.connection.execute(
            "SELECT source_relative_path, status, error_category FROM extraction_runs WHERE attempted_route='docx_native' ORDER BY source_relative_path"
        ).fetchall()
    finally:
        registry.close()
    assert runs == [("broken.docx", "failed", "docx_extraction_error"), ("valid.docx", "successful", None)]


def test_pdf_catalog_is_file_level_while_pages_and_tables_remain_children(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    pdf = source / "mixed.pdf"
    write_pdf(pdf, [{"texts": [(72, 72, "native page one")]}, {"texts": [(72, 72, "native page two")]}])
    before = _sha256(pdf)

    _process(source, workspace)

    catalog = CatalogService(registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    files = catalog.list_files()
    assert files["pagination"]["total"] == 1
    assert files["items"][0]["displayName"] == "mixed.pdf"
    detail = catalog.file_detail(files["items"][0]["fileId"])
    assert detail is not None
    assert len(detail["pages"]) == 2
    assert [page["pageNumber"] for page in detail["pages"]] == [1, 2]
    assert _sha256(pdf) == before


def test_support_matrix_defers_office_and_web_formats_without_marking_catalog_failure() -> None:
    common = dict(file_id="f", mime_like_type="application/octet-stream", routing_class="document")
    for detected, extension, expected_reason in (
        ("pptx", ".pptx", "deferred_pptx"),
        ("ole_compound", ".doc", "deferred_doc"),
        ("plain_text", ".html", "unsupported_business_format"),
    ):
        plan = plan_processing(RegistryFileInfo(detected_type=detected, observed_extension=extension, **common))
        assert plan.support_status is SupportStatus.UNSUPPORTED
        assert plan.reason_code == expected_reason
        assert plan.business_format is None
    assert plan_processing(RegistryFileInfo(detected_type="docx", observed_extension=".docx", **common)).business_format is BusinessFormat.DOCX


def test_reset_and_release_contracts_are_narrow_and_exclude_development_data() -> None:
    root = Path(__file__).parents[1]
    reset = (root / "scripts" / "reset_workspace.ps1").read_text(encoding="utf-8")
    release = (root / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
    assert '(Join-Path $repoRoot "workspace\\artifacts")' in reset
    assert '(Join-Path $repoRoot "cache")' in reset
    assert 'workspace\\input' in reset
    assert "workspace\\state\\registry.duckdb" in release
    assert "workspace\\artifacts" in release
    directory_copies = release.split("$directoryCopies", 1)[1].split("foreach", 1)[0]
    assert "cache" not in directory_copies
    assert "workspace" not in directory_copies


def test_reset_workspace_removes_generated_data_but_preserves_source_input(tmp_path: Path) -> None:
    project = tmp_path / "project"
    for name in ("src", "runtime", "models", "config", "workspace", "cache"):
        (project / name).mkdir(parents=True)
    source = project / "workspace" / "input" / "user-source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("user evidence", encoding="utf-8")
    generated = project / "workspace" / "artifacts" / "generated.bin"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"generated")
    cached = project / "cache" / "uv" / "generated.bin"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached")
    script_dir = project / "scripts"
    script_dir.mkdir()
    script = script_dir / "reset_workspace.ps1"
    script.write_text(
        (Path(__file__).parents[1] / "scripts" / "reset_workspace.ps1").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Force"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert source.read_text(encoding="utf-8") == "user evidence"
    assert not generated.exists()
    assert not cached.exists()

class _ConnectionHandler(BaseHTTPRequestHandler):
    mode = "ok"
    bodies: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).bodies.append(body)
        if type(self).mode == "timeout":
            time.sleep(2.0)
            return
        status = {"auth": 401, "endpoint": 404}.get(type(self).mode, 200)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if status == 200:
            content = json.dumps({"ok": True}, ensure_ascii=False)
            response = {"choices": [{"message": {"content": content}}]}
        else:
            response = {"error": {"message": "synthetic failure"}}
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def connection_server():
    _ConnectionHandler.mode = "ok"
    _ConnectionHandler.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ConnectionHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _configured_project(tmp_path: Path, base_url: str, *, timeout: int = 1) -> Path:
    project = tmp_path / "project"
    ProjectAIConfigStore(project).save(
        base_url=base_url,
        api_key="synthetic-connection-key",
        model="synthetic-model",
        timeout=timeout,
        vision_enabled=False,
    )
    return project


def test_fake_openai_connection_normalizes_base_url_and_sends_no_workspace_data(tmp_path: Path, connection_server) -> None:
    port = connection_server.server_address[1]
    for index, base_url in enumerate((
        f"http://127.0.0.1:{port}",
        f"http://127.0.0.1:{port}/v1",
        f"http://127.0.0.1:{port}/v1/chat/completions",
    )):
        project = _configured_project(tmp_path / str(index), base_url, timeout=5)
        app = BackendApp(project_root=project, registry_path=project / "workspace/state/registry.duckdb", workspace_root=project / "workspace")
        try:
            response = app.handle_api("POST", "/api/v1/settings/ai/test", {}, b"{}")
        finally:
            app.close(timeout=5)
        assert response.status == 200
        assert response.payload["connectionStatus"] == "CONNECTED"
    assert len(_ConnectionHandler.bodies) == 3
    assert all(b"workspace" not in body.lower() for body in _ConnectionHandler.bodies)
    assert all(b"synthetic-connection-key" not in body for body in _ConnectionHandler.bodies)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("auth", "AUTH_FAILED"), ("endpoint", "ENDPOINT_NOT_FOUND"), ("timeout", "TIMEOUT")],
)
def test_fake_openai_connection_errors_are_classified(tmp_path: Path, connection_server, mode: str, expected: str) -> None:
    _ConnectionHandler.mode = mode
    port = connection_server.server_address[1]
    project = _configured_project(tmp_path, f"http://127.0.0.1:{port}/v1", timeout=1)
    app = BackendApp(project_root=project, registry_path=project / "workspace/state/registry.duckdb", workspace_root=project / "workspace")
    try:
        with pytest.raises(ApiError) as raised:
            app.handle_api("POST", "/api/v1/settings/ai/test", {}, b"{}")
    finally:
        app.close(timeout=5)
    assert raised.value.code == expected


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [("model_not_found", "MODEL_NOT_FOUND"), ("invalid_response", "INVALID_RESPONSE"), ("connection_error", "CONNECTION_FAILED")],
)
def test_connection_error_mapping_is_provider_neutral(tmp_path: Path, provider_code: str, expected: str) -> None:
    class FailureProvider:
        def generate(self, _request):
            raise SemanticProviderError("synthetic failure", code=provider_code)

    project = _configured_project(tmp_path, "http://127.0.0.1:9/v1", timeout=1)
    app = BackendApp(
        project_root=project,
        registry_path=project / "workspace/state/registry.duckdb",
        workspace_root=project / "workspace",
        semantic_provider=FailureProvider(),
    )
    try:
        with pytest.raises(ApiError) as raised:
            app.handle_api("POST", "/api/v1/settings/ai/test", {}, b"{}")
    finally:
        app.close(timeout=5)
    assert raised.value.code == expected
