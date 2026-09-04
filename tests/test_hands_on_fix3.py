from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from chongzu.api.app import ApiError, BackendApp
from chongzu.services.process import ProcessTaskManager, TaskAdmissionError
import chongzu.services.reset as reset_module
from chongzu.services.catalog import CatalogService
from chongzu.clean import process_source
from tests.xlsx_factory import write_xlsx


def _docx(path: Path, paragraphs: list[str]) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f"<w:p><w:r><w:t>{value}</w:t></w:r></w:p>"
        for value in paragraphs
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("word/document.xml", document)


def _empty_project_config(project: Path) -> Path:
    path = project / "config" / "llm.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "base_url": "",
                "api_key": "",
                "model": "",
                "timeout_seconds": 120,
                "vision_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_reset_http_keeps_real_child_stdout_handle_usable_and_reinitializes_views(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = project / "workspace"
    source = workspace / "input" / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("source evidence", encoding="utf-8")
    config = _empty_project_config(project)
    log_path = workspace / "logs" / "server.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("before reset\n", encoding="utf-8")
    (workspace / "artifacts" / "generated").mkdir(parents=True)
    (workspace / "artifacts" / "generated" / "asset.bin").write_bytes(b"generated")
    (project / "cache" / "generated").mkdir(parents=True)
    (project / "cache" / "generated" / "cache.bin").write_bytes(b"generated")

    app = BackendApp(project_root=project, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    log_handle = log_path.open("a", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; print('child stdout', flush=True); time.sleep(3)"],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        response = app.handle_api(
            "POST",
            "/api/v1/workspace/reset",
            {},
            json.dumps({"confirmation": "清空"}).encode(),
            request_id="req_fix3_reset",
        )
        assert response.status == 200
        assert response.payload["reset"] is True
        assert log_path.is_file()
        assert child.poll() is None
        log_handle.write("after reset\n")
        log_handle.flush()
        assert "after reset" in log_path.read_text(encoding="utf-8")
        assert source.read_text(encoding="utf-8") == "source evidence"
        assert config.is_file()
        assert (workspace / "artifacts" / "cleaning").is_dir()
        assert (project / "cache" / "temp" / "pycache").is_dir()
        assert app.catalog.overview()["files"] == 0
        assert app.catalog.list_files(limit=10)["items"] == []
        assert app.quality.list_issues(limit=10)["items"] == []

        # The operation is safe to repeat without a process restart.
        second = app.handle_api(
            "POST",
            "/api/v1/workspace/reset",
            {},
            json.dumps({"confirmation": "清空"}).encode(),
            request_id="req_fix3_reset_again",
        )
        assert second.status == 200
        assert app.catalog.overview()["files"] == 0
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        log_handle.close()
        app.close(timeout=5)


def test_reset_failure_reports_stage_and_completed_phases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    workspace = project / "workspace"
    _empty_project_config(project)
    blocked = workspace / "artifacts" / "blocked"
    blocked.mkdir(parents=True)
    (blocked / "data.bin").write_bytes(b"data")
    app = BackendApp(project_root=project, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)

    def fail_rmtree(_path: object) -> None:
        raise PermissionError("synthetic active handle")

    monkeypatch.setattr(reset_module.shutil, "rmtree", fail_rmtree)
    try:
        with pytest.raises(ApiError) as raised:
            app.handle_api(
                "POST",
                "/api/v1/workspace/reset",
                {},
                json.dumps({"confirmation": "清空"}).encode(),
                request_id="req_fix3_partial",
            )
    finally:
        app.close(timeout=5)
    error = raised.value
    assert error.code == "RESET_FAILED"
    assert error.stage == "artifact_clear"
    assert error.details["completedPhases"] == ["prepare", "registry_clear"]
    assert "PermissionError" in (error.diagnostic or "")


def test_reset_admission_barrier_rejects_process_analysis_and_report_submissions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    manager = ProcessTaskManager.for_paths()
    manager.begin_reset()
    try:
        with pytest.raises(TaskAdmissionError):
            manager.submit(source)
        with pytest.raises(TaskAdmissionError):
            manager.submit_analysis("analysis", lambda *_args: {})
        with pytest.raises(TaskAdmissionError):
            manager.submit_report("report", lambda *_args: {})
    finally:
        manager.end_reset()
        manager.shutdown(timeout=1)


def test_save_replace_failure_is_classified_without_changing_existing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    config = _empty_project_config(project)
    before = config.read_bytes()
    app = BackendApp(project_root=project, registry_path=project / "workspace/state/registry.duckdb", workspace_root=project / "workspace")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("chongzu.semantic.settings.os.replace", fail_replace)
    try:
        with pytest.raises(ApiError) as raised:
            app.handle_api(
                "PUT",
                "/api/v1/settings/ai",
                {},
                json.dumps({"baseUrl": "http://draft.invalid/v1", "model": "draft-model", "timeout": 10, "visionEnabled": False}).encode(),
                request_id="req_fix3_save",
            )
    finally:
        app.close(timeout=5)
    error = raised.value
    assert error.code == "CONFIG_REPLACE_FAILED"
    assert error.category == "CONFIG_REPLACE_FAILED"
    assert error.stage == "replace"
    assert "OSError" in (error.diagnostic or "")
    assert config.read_bytes() == before


def test_docx_long_document_is_one_continuous_bounded_reading_segment(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    _docx(source / "long.docx", [f"paragraph {index}" for index in range(144)])
    process_source(source, workers=1, force=True, registry_path=workspace / "state/registry.duckdb", workspace_root=workspace)
    catalog = CatalogService(registry_path=workspace / "state/registry.duckdb", workspace_root=workspace)
    file_id = catalog.list_files(limit=10)["items"][0]["fileId"]
    content = catalog.file_content(file_id)
    assert content is not None
    blocks = content["sections"][0]["blocks"]
    text_blocks = [block for block in blocks if block["type"] == "text"]
    assert len(text_blocks) == 1
    assert "paragraph 0" in text_blocks[0]["text"]
    assert "paragraph 143" in text_blocks[0]["text"]
    assert len(text_blocks[0]["text"]) <= content["limits"]["maxTextChars"]


def test_xlsx_file_presentation_defaults_to_raw_preview_and_keeps_sheets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    write_xlsx(
        source / "book.xlsx",
        [
            ("Zeta", [["Name", "Value"], ["n" * 3_000, 1]], None),
            ("Alpha", [["Other", "Value"], ["south", 2]], None),
        ],
    )
    process_source(source, workers=1, force=True, registry_path=workspace / "state/registry.duckdb", workspace_root=workspace)
    catalog = CatalogService(registry_path=workspace / "state/registry.duckdb", workspace_root=workspace)
    file_id = catalog.list_files(limit=10)["items"][0]["fileId"]
    content = catalog.file_content(file_id)
    assert content is not None
    assert [section["sheetName"] for section in content["sections"]] == ["Zeta", "Alpha"]
    previews = [block for section in content["sections"] for block in section["blocks"]]
    assert all(block["previewLayer"] == "raw" for block in previews)
    assert all(block["preview"]["layer"] == "raw" for block in previews)
    assert any(block["preview"].get("cellValuesTruncated") for block in previews)


def test_fix3_frontend_uses_one_advanced_text_link_and_settings_layout() -> None:
    root = Path(__file__).parents[1]
    app = (root / "frontend/src/App.tsx").read_text(encoding="utf-8")
    styles = (root / "frontend/src/styles.css").read_text(encoding="utf-8")
    assert "查看文本资产详情" not in app
    assert app.count("高级文本详情") == 1
    assert "settings-layout" in app
    assert ".settings-layout" in styles
    assert "position: sticky" in styles and "top: 0" in styles
