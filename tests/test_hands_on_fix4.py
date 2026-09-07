"""Integrated contracts for the fourth hands-on repair pass."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from chongzu.api.app import BackendApp
from chongzu.api.lifecycle import stop_server
from chongzu.api.server import ChongZuHTTPServer
from chongzu.clean import process_source
from chongzu import paths
from chongzu.semantic.settings import ProjectAIConfigStore, load_runtime_ai_settings
from tests.pdf_factory import write_pdf


def _json_request(
    base: str,
    path: str,
    *,
    method: str = "GET",
    payload: object | None = None,
) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "frontend" / "dist").mkdir(parents=True)
    (project / "frontend" / "dist" / "index.html").write_text("<!doctype html><title>ChongZu</title>", encoding="utf-8")
    config = project / "config" / "llm.json"
    config.parent.mkdir(parents=True)
    config.write_text(
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
    (project / "workspace" / "input").mkdir(parents=True)
    return project


@contextmanager
def _threaded_server(project: Path):
    workspace = project / "workspace"
    app = BackendApp(
        project_root=project,
        registry_path=workspace / "state" / "registry.duckdb",
        workspace_root=workspace,
        frontend_dist=project / "frontend" / "dist",
    )
    server = ChongZuHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        app.close(timeout=5)
        thread.join(timeout=5)


def _wait_for_health(base: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, payload = _json_request(base, "/api/v1/health")
            if status == 200:
                return payload
        except (OSError, URLError, ValueError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"server did not become healthy: {last_error!r}")


def _start_bundled_server(project: Path, port: int, monkeypatch: pytest.MonkeyPatch) -> subprocess.CompletedProcess[str]:
    repo = Path(__file__).parents[1]
    executable = project / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME / "python.exe"
    cache = project / "cache" / "temp"
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            item for item in (str(repo / "src"), str(repo / "runtime" / "packages"), os.environ.get("PYTHONPATH", "")) if item
        ),
    )
    monkeypatch.setenv("CHONGZU_PROJECT_ROOT", str(project))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(cache / "pycache"))
    monkeypatch.setenv("TMP", str(cache))
    monkeypatch.setenv("TEMP", str(cache))
    return subprocess.run(
        [
            str(executable),
            "-m",
            "chongzu.api.lifecycle",
            "start",
            "--project-root",
            str(project),
            "--port",
            str(port),
            "--no-browser",
        ],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=40,
        check=False,
    )


def test_live_server_reset_preserves_runtime_controls_and_restart_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the actual lifecycle child, active lock, log, and HTTP reset."""

    project = _make_project(tmp_path)
    source = project / "workspace" / "input" / "source.txt"
    source.write_text("immutable source", encoding="utf-8")
    config = project / "config" / "llm.json"
    config_before = config.read_bytes()
    (project / "workspace" / "artifacts" / "old").mkdir(parents=True)
    (project / "workspace" / "artifacts" / "old" / "asset.bin").write_bytes(b"generated")
    (project / "workspace" / "output" / "old").mkdir(parents=True)
    (project / "workspace" / "output" / "old" / "result.json").write_text("generated", encoding="utf-8")
    (project / "cache" / "previews" / "old").mkdir(parents=True)
    (project / "cache" / "previews" / "old" / "page.png").write_bytes(b"generated")

    port = _free_port()
    state = project / "workspace" / "state" / "server.pid"
    lock = project / "workspace" / "state" / "server.lock"
    start_lock = project / "workspace" / "state" / "server.start.lock"
    log = project / "workspace" / "logs" / "server.log"
    running = False
    try:
        runtime_target = project / "runtime" / "python" / paths.PYTHON_RUNTIME_DIRNAME
        runtime_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            paths.PYTHON_RUNTIME_DIR,
            runtime_target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        started = _start_bundled_server(project, port, monkeypatch)
        assert started.returncode == 0, started.stdout + started.stderr
        running = True
        _wait_for_health(f"http://127.0.0.1:{port}")
        assert state.is_file() and lock.is_file() and start_lock.is_file() and log.is_file()

        status, reset = _json_request(
            f"http://127.0.0.1:{port}",
            "/api/v1/workspace/reset",
            method="POST",
            payload={"confirmation": "清空"},
        )
        assert status == 202, reset
        assert reset["accepted"] is True
        assert reset["restarting"] is True
        assert reset["requestId"]
        assert _wait_for_health(f"http://127.0.0.1:{port}")["registry"]["status"] == "ready"
        reset_result = None
        for _ in range(40):
            try:
                status, candidate = _json_request(f"http://127.0.0.1:{port}", reset["statusUrl"])
            except (URLError, ValueError):
                time.sleep(0.25)
                continue
            assert status == 200
            reset_result = candidate
            if candidate["result"] in {"succeeded", "failed"}:
                break
            time.sleep(0.25)
        assert reset_result is not None
        assert reset_result["phase"] == "completed"
        assert reset_result["result"] == "succeeded"
        assert state.is_file() and lock.is_file() and start_lock.is_file() and log.is_file()
        assert not (project / "workspace" / "artifacts" / "old").exists()
        assert not (project / "workspace" / "output" / "old").exists()
        assert not (project / "cache" / "previews" / "old").exists()
        assert source.read_text(encoding="utf-8") == "immutable source"
        assert config.read_bytes() == config_before
        status, overview = _json_request(f"http://127.0.0.1:{port}", "/api/v1/overview")
        assert status == 200 and overview["files"] == 0
        status, catalog = _json_request(f"http://127.0.0.1:{port}", "/api/v1/catalog")
        assert status == 200 and catalog["items"] == []

        # Save is verified over the real PUT adapter, without a provider call.
        status, saved = _json_request(
            f"http://127.0.0.1:{port}",
            "/api/v1/settings/ai",
            method="PUT",
            payload={
                "baseUrl": "http://127.0.0.1:9/v1",
                "apiKey": "synthetic-only-key",
                "model": "synthetic-model",
                "timeout": 3,
                "visionEnabled": False,
            },
        )
        assert status == 200, saved
        assert saved["saved"] is True
        assert saved["settings"]["apiKeyConfigured"] is True
        assert "synthetic-only-key" not in json.dumps(saved)

        assert stop_server(project) == 0
        running = False
        assert not state.exists()
        started = _start_bundled_server(project, port, monkeypatch)
        assert started.returncode == 0, started.stdout + started.stderr
        running = True
        _wait_for_health(f"http://127.0.0.1:{port}")
        status, settings = _json_request(f"http://127.0.0.1:{port}", "/api/v1/settings/ai")
        assert status == 200
        assert settings["settings"]["model"] == "synthetic-model"
        assert settings["settings"]["apiKeyConfigured"] is True

        status, second_reset = _json_request(
            f"http://127.0.0.1:{port}",
            "/api/v1/workspace/reset",
            method="POST",
            payload={"confirmation": "清空"},
        )
        assert status == 202, second_reset
        assert second_reset["accepted"] is True
        assert _wait_for_health(f"http://127.0.0.1:{port}")["server"]["pid"]
        second_result = None
        for _ in range(40):
            try:
                status, candidate = _json_request(f"http://127.0.0.1:{port}", second_reset["statusUrl"])
            except (URLError, ValueError):
                time.sleep(0.25)
                continue
            assert status == 200
            second_result = candidate
            if candidate["result"] in {"succeeded", "failed"}:
                break
            time.sleep(0.25)
        assert second_result is not None and second_result["result"] == "succeeded"
        assert config.is_file()
    finally:
        if running:
            assert stop_server(project) == 0


def test_real_put_save_persists_without_returning_secret(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    with _threaded_server(project) as base:
        status, payload = _json_request(
            base,
            "/api/v1/settings/ai",
            method="PUT",
            payload={
                "baseUrl": "http://provider.invalid/v1",
                "apiKey": "synthetic-put-key",
                "model": "synthetic-model",
                "timeout": 4,
                "visionEnabled": False,
            },
        )
        assert status == 200, payload
        assert payload["saved"] is True
        assert "synthetic-put-key" not in json.dumps(payload)
        status, current = _json_request(base, "/api/v1/settings/ai")
        assert status == 200
        assert current["settings"]["model"] == "synthetic-model"
        assert current["settings"]["apiKeyConfigured"] is True
    runtime = load_runtime_ai_settings(project)
    assert runtime.config.model == "synthetic-model"
    assert runtime.api_key_configured is True


def test_pdf_file_content_has_lazy_original_page_preview(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    write_pdf(source / "document.pdf", [{"texts": [(50, 80, "Title"), (50, 120, "Body text")]}])
    process_source(source, workers=1, force=True, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    app = BackendApp(project_root=tmp_path, registry_path=workspace / "state" / "registry.duckdb", workspace_root=workspace)
    try:
        catalog = app.catalog.list_files(limit=10)
        file_id = catalog["items"][0]["fileId"]
        content = app.catalog.file_content(file_id)
        assert content is not None
        assert content["sourcePreview"]["kind"] == "pdf_page"
        rendered, content_type = app.catalog.file_preview(file_id, page=1)
        assert content_type == "image/png"
        assert rendered.startswith(b"\x89PNG")
        assert (tmp_path / "cache" / "previews").is_dir()
    finally:
        app.close(timeout=5)


def test_fix4_frontend_presentation_settings_and_runtime_contracts() -> None:
    root = Path(__file__).parents[1]
    app = (root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    api = (root / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    styles = (root / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
    hardening = (root / "frontend" / "src" / "hardening.css").read_text(encoding="utf-8")

    assert "FileContentTableBlock" in app
    assert "继续加载" in app
    assert "sourcePreview" in app and "source-page-preview" in app
    assert app.count("文本技术详情") == 1
    assert "查看文本资产详情" not in app
    assert "method: \"PUT\"" in api
    assert "catalogRequestGeneration" in app
    assert "catalog-error" in app
    assert "onRetry={() => void refreshCatalog()}" in app
    assert "settings-load-error" in app
    assert "aiSettingsRequestGeneration" in app
    assert ".settings-page .settings-layout" in styles
    assert "settings-panel { max-width: 720px" not in hardening
    assert ".settings-page .settings-panel" in hardening
    assert "position: sticky" in styles and "top: 0" in styles


@pytest.mark.parametrize("name", ("server.lock", "server.start.lock", "server.pid"))
def test_runtime_control_names_are_centralized_and_excluded_from_reset(name: str) -> None:
    from chongzu import paths

    assert name in paths.ACTIVE_RUNTIME_CONTROL_FILE_NAMES
